"""Define ``klt erc``: the layer-by-layer connectivity model (issue #859,
Phase 1a), the per-gate antenna-ratio verdict (issue #860, Phase 1b), the
core ERC finding checks (issue #861, Phase 1c), and antenna-violation fix
guidance (issue #908, Phase 3) -- all part of the antenna + ERC signoff epic
#713.

Pure library: :func:`run_erc` returns plain Python data (a JSON-serialisable
``dict``) and never prints -- serialisation and human-readable formatting
live in ``cli/erc_cmd.py``, matching every other ``klt`` verb (see
``layers.py``'s docstring on the same convention).

Scope (see ``docs/cli/erc.md`` for the full picture): ``klt erc``'s full,
intended interface (per epic #713) is JSON in (a routed layout, a netlist,
and the PDK's antenna/ERC rules) / JSON out (a per-gate antenna-ratio
verdict citing the PDK limit, plus an ERC finding list -- floating gate,
unconnected/multiply-driven net, missing tie, supply short). **Phase 1a**
(#859) delivered the interface and the layer-by-layer connectivity model --
the per-gate accumulation of connected metal area at each fabrication step
-- that both 1b's and 1c's checks below consume. **Phase 1b** (#860) adds
the antenna-ratio verdict: each level's ``antenna_ratio``
(``cumulative_area_um2 / gate_area_um2``) compared against a real PDK
antenna-ratio limit (``--pdk``), with the limit and its source citation
echoed on every checked level -- see :data:`_SKY130_ANTENNA_RATIO_MAX_EGAR`
below. **Phase 1c** (#861) additively delivers ``erc_findings``: the four
core electrical-correctness rules (floating gate, unconnected/multiply-
driven net, missing substrate/well tie, supply short), computed from the
same connectivity model plus two new optional spec sections (``nets``,
``ties``) -- see "ERC finding checks" below and "Phase scope" /
"ERC finding checks" in ``docs/cli/erc.md``. **Phase 3** (#908) additively
delivers ``levels[].remedy``: for every ``verdict: "violate"`` level, a
standard-fix recommendation (diode insertion, or layer jumping when a
neighbouring level has margin) naming the specific net and layer -- see
:func:`_antenna_remedy`.

Connectivity: geometry is traced with ``klayout.db.LayoutToNetlist`` used
purely for wire/via connectivity (no device *extraction* is registered --
but see the optional ``devices`` spec section below, issue #2183, which
lets a spec declare where a drawn device body sits so it stops reading as
a wire) -- exactly the same API ``extract.py``'s own metal/via
connectivity graph and ``power.py``'s ``run_power`` already use, scoped
down to only the caller-declared gate + stackup layers (plus, in a
*separate* graph used only for the tie check, each declared tie's derived
tap sites -- see
:func:`_extract_connectivity`). This is the "LVS's shared
net extraction" 1c's own issue description names as the connectivity model
1a builds and 1c reuses -- ``LayoutToNetlist`` is the same engine
``extract.py``'s device-aware netlist extraction and ``klt lvs``'s
comparison both sit on top of, used here without device recognition,
exactly as ``power.py`` already established for a sibling connectivity-only
verb.

"Per gate" means "per electrically distinct net whose geometry includes the
declared gate-role layer" -- not "per individually drawn poly finger". This
matches real process-antenna-area-ratio (PAAR) methodology directly:
antenna charge accumulates across an entire electrically connected net, so
two transistor gates tied together by the same poly/metal net are correctly
one accumulation, not two. A gate is therefore auto-discovered from
connectivity -- unlike ``klt power``'s caller-named ``power_nets``, this
spec never requires a ``label_layer`` to identify which net is "a gate";
every net that includes gate-role geometry becomes one entry.

ERC finding checks (issue #861, Phase 1c), each with no device-recognition
dependency -- purely geometric/connectivity, matching this module's Phase
1a posture:

- **Floating gate** (``erc.floating_gate``): a gate net (from the existing
  ``gates[]`` model) whose accumulation stops immediately after the gate
  role -- zero connected area on every ``stackup`` role above it. This is
  the electrical signature of an uncontacted/floating gate, computed
  directly from ``gates[]`` with no additional spec section required.
- **Unconnected / multiply-driven net** (``erc.unconnected_net`` /
  ``erc.multiply_driven_net``): driven by the new optional ``nets`` spec
  section (named nets to check, mirroring ``klt power``'s ``power_nets``
  but with an added ``kind``). A declared net matching zero or more than
  one disconnected electrical island is "unconnected" -- the expected
  count is the entry's declared ``islands`` (issue #2400, default 1, so
  the pre-#2400 "one label string == one net" model is the undeclared
  form) -- and when the actual count is more than one, the finding
  carries an ``islands[]`` entry locating each
  island (issue #2194, see :func:`_island_entry`); two *different*
  declared net names that resolve to the same electrical island are
  "multiply-driven" (shorted together) -- or, when both are declared
  ``"kind": "supply"``, a **supply short** (``erc.supply_short``) instead.
- **Missing substrate/well tie** (``erc.missing_tie``): driven by the new
  optional ``ties`` spec section (each entry names a well/tub layer, a tap
  layer -- optionally narrowed to a boolean by ``tap_requires``, issue
  #2169 -- the ``stackup`` role the tap connects up to, and the supply net
  it must reach). Every physically distinct well/tub shape must contain at
  least one tap that is electrically connected (via the tie connectivity
  graph, see :func:`_extract_connectivity`) to the declared net; a missing
  tap, or taps none of which reach the declared net, is reported. The
  declared well is *not* a conductor in that graph -- it contributes only
  where its taps sit -- and the graph is extracted separately from the one
  ``gates[]`` is built on, so no ``ties`` declaration can reach the antenna
  half of the same report (issue #2169). A tie whose declared tap region is
  **indistinguishable from an ordinary source/drain contact** -- no
  ``tap_requires`` narrowing, no ``tap_is_dedicated`` affirmation -- is
  reported as *skipped* work rather than as a passing check (issue #2199,
  see :func:`_degenerate_tie_reasons`).

  **Caller-asserted taps** (``ties[].tap_boxes``, issue #2234): a stream
  whose taps are genuinely drawn but carry no distinguishing implant/marker
  layer at all -- so neither ``tap_requires`` nor ``tap_is_dedicated`` has
  anything true to narrow or affirm -- can instead name the tap geometry
  directly, as a list of ``[left, bottom, right, top]`` micrometre boxes
  intersected into ``tap_layer``. This is caller *assertion* rather than
  layer-derived narrowing, so it is graded under its own coverage
  classification (``erc_coverage.checked_by_assertion``, see
  :func:`_connectivity_coverage`) instead of being folded into an ordinary
  geometrically-derived pass -- and it is held to the same falsifiability
  bar as every other narrowing form: an assertion that removes nothing from
  the drawn ``tap_layer`` inside the well is exactly as degenerate as an
  omitted ``tap_requires`` (issue #2199's test is geometric, not "which key
  was given"), and an assertion matching no drawn geometry at all produces
  an honest "no tap" finding rather than a silent pass.

  **Caller-asserted substrate regions** (``ties[].well_layer: null`` +
  ``ties[].well_boxes``, issue #2255): a block sitting in a *native
  substrate* -- NMOS-in-bulk, with no drawn pwell/tub shape anywhere in the
  stream because the well is diffusion-derived rather than layer-marked --
  has nothing to name as ``well_layer`` at all, so before this it could not
  declare its substrate tie in any form. Such a spec sets ``well_layer`` to
  ``null`` and names the substrate region it is claiming directly, as the
  same ``[left, bottom, right, top]`` micrometre box list ``tap_boxes``
  uses. Unlike ``tap_boxes``, which *narrows* an already-required drawn
  layer, this **substitutes** for one -- so it carries its own falsifiability
  test and its own coverage classification
  (``erc_coverage.checked_by_well_assertion``, see
  :func:`_connectivity_coverage`), never folded into either an ordinary
  geometrically-derived pass or the tap-side ``checked_by_assertion`` list.
  The test: an asserted region that covers (to within
  :data:`_WELL_ASSERTION_MIN_UNCOVERED_FRACTION` of) the **whole top-cell
  extent** is indistinguishable from "the entire die is the substrate",
  which makes ``erc.missing_tie`` trivially satisfiable by any contact
  anywhere that reaches the declared net -- exactly the unfalsifiable pass
  issue #2199 exists to rule out -- so it is graded as *skipped* work
  (:data:`REASON_DEGENERATE_WELL_ASSERTION`, see
  :func:`_degenerate_tie_reasons`) rather than accepted as evidence.

  **Well-side class selection** (``ties[].well_requires`` /
  ``ties[].well_excludes``, issue #2339): one drawn well/tub layer can
  legitimately carry two differently-biased well *classes* -- device-body
  wells strapped to the positive supply beside the base tub of a vertical
  bipolar strapped to the other rail, an ordinary shape for a bandgap or
  bias block on any PDK with a single n-tub layer. Every narrowing key
  above acts on the **tap**, so before this a spec could name the tub layer
  only as a whole: each ``ties[]`` entry graded *every* merged shape on it
  against that entry's single ``net``, and whichever class was not declared
  reported a false ``erc.missing_tie`` -- one per correctly-tapped well of
  the other class. These two keys narrow the **well** side instead: each
  merged shape of ``well_layer`` is kept only when it interacts with
  geometry on every ``well_requires`` layer and with none of the
  ``well_excludes`` layers, so a PDK device marker over one class selects
  it and a second, complementary entry (the same marker under
  ``well_excludes``) selects the rest. This *selects whole drawn shapes*
  rather than intersecting the layer (which is what ``tap_requires`` does
  on the tap side): the per-well unit :func:`_tie_findings` checks stays
  the shape the layout actually draws, so a marker covering only part of a
  tub cannot push that tub's real tap outside the region being graded.
  Drawn-geometry narrowing, not caller assertion, so it earns no assertion
  bucket -- and it carries the same falsifiability bar every narrowing form
  here does: a declared selection that keeps *every* drawn shape, or none
  of them, is graded as *skipped* work
  (:data:`REASON_DEGENERATE_WELL_SELECTION`, see
  :func:`_degenerate_tie_reasons`) rather than as a pass.

  **Disclosed non-declaration** (top-level ``ties_disclosure``, issues
  #2234 and #2247): a run that deliberately declares no tap can say so
  explicitly, and say *why*, instead of simply omitting ``ties`` -- via a
  top-level ``{"reason": "<why>", "kind": "<which obstacle>"}`` spec key.
  This changes no finding and no geometry; it only changes the
  ``erc_coverage.inapplicable`` reason recorded for the undeclared
  ``erc.missing_tie`` work, so a consumer (``klt signoff``'s T1 item 11, see
  ``docs/design-evidence-tiers.md``) can tell a considered, disclosed
  omission from "nobody declared ties at all" -- states that previously
  rendered identically. Two obstacles are expressible, because they have
  different remedies: ``"unexpressible"`` (the default, issue #2234 --
  nothing drawn to narrow, affirm, or bound, so there is no tap to name;
  reason ``"ties_disclosed_unexpressible"``) and ``"tool_limitation"``
  (issue #2247 -- the tap is nameable, but the `klt` build this evidence
  must be produced on cannot grade a declared tie safely, the reported
  instance being issue #2169's unisolated tie extraction turning a correct
  ``ties[]`` into a false ``erc.supply_short``; reason
  ``"ties_disclosed_tool_limitation"``). Neither is verified by this run --
  a disclosure is the caller's word, which is why it never moves a finding
  and why item 11 stays unmet on both -- but the report's own
  ``provenance.klt_version`` pins the build, so a reader can check whether a
  disclosed tool limitation applies to the run in front of them.

Device bodies (``devices``, issue #2183): a conductor role carries
*geometry*, and nothing in the model above distinguishes a wire from a
drawn device body sitting on the same layer -- a poly resistor, a poly
fuse, a MiM/MOM capacitor plate. A rail-to-rail device string (the
defining topology of a power-on-reset comparator, a brown-out detector, a
supply-referenced bias string) therefore reads as a dead metal short
between the two supplies it deliberately spans, and reports a false
``erc.supply_short``. The optional ``devices`` array is how a spec says
where those bodies are: each entry names a device-body marker layer (the
``RES_MK``/``SAB``/``Resistor``, ``CAP_MK``/``MIM_L_MK``/``FuseTop``-style
layer a PDK deck already uses for device recognition, and which
``extract.py`` already consults) and the ``stackup``/``vias`` role it sits
on, and that region is **subtracted** from the role's conductor region
before it is registered -- so the body breaks the net instead of bridging
it. See :func:`klayout_tools._devices._device_body_cuts` -- the whole
``devices[]`` fragment (schema, validation, subtraction, area accounting)
lives in ``_devices.py``, shared verbatim with ``klt power`` (issue #2260)
so a caller can hand the same declaration to either verb.

Deck-driven device-marker auto-detection (``--deck``, issue #2204): hand-
transcribing a PDK's device-body marker layer/datatype into ``devices[]``
is a silent-failure risk -- a mis-transcription subtracts nothing, and the
only signal is ``provenance.devices[].body_area_um2 == 0.0`` (plus, since
issue #2226, a stderr warning whenever the mis-transcribed marker layer is
nonetheless drawn somewhere on this layout). When
``--deck`` names a curated extraction deck (the same name-keyed registry
``klt extract``/``klt lvs`` resolve, no PDK install needed), that deck's own
``ResistorDevice``/``CapacitorDevice`` declarations are matched against the
declared ``stackup``/``vias`` roles by *exact* conducting-body-layer
equality and auto-carved out the same way an explicit ``devices[]`` entry
would be -- see :func:`_deck_device_cuts` for the matching rule and
:func:`_resolve_deck`. An explicit ``devices[]`` entry for a role still
wins over the deck's own auto-detection for that role. Every auto-applied
carve-out is echoed in ``provenance.devices`` exactly as a hand-declared one
is, plus ``"source"``/``"superseded_by"`` fields distinguishing it -- see
``run_erc``. Omitting ``--deck`` (every caller before this issue) leaves
every output byte-identical to before this feature existed.

Findings-only runs (``--findings-only``, issue #2219): the per-gate
per-level accumulation above is what ``klt erc`` spends nearly all of its
time on -- one ``polygons_of_net(...).merged().area()`` for every gate net
on every ``stackup`` role -- and a caller who only wants ``erc_findings``
(the structural supply read: ``nets[]`` island/short checks plus ``ties[]``)
pays it for verdicts that, without a ``--pdk`` antenna-limit table, grade
nothing at all. ``--findings-only`` skips that walk. Every
``erc_findings`` rule still runs and every finding is identical: the four
``nets[]``/``ties[]``-driven rules never read the accumulated areas, and
``erc.floating_gate`` -- the one rule that does -- is evaluated from the
same connectivity graph by :func:`_gate_is_floating` instead, which stops
at the first role carrying area rather than measuring every role. The
antenna half then reports what it actually did: ``null`` accumulation
fields, ``"unchecked"`` verdicts, every level in ``coverage.skipped`` with
reason :data:`REASON_FINDINGS_ONLY`, and ``status: "not_checked"``.

See ``docs/cli/erc.md`` for the full spec-file schema and JSON contract.
"""

from __future__ import annotations

import sys
from typing import Any

from ._devices import (
    _conductor_role_layers,
    _cut_device_bodies,
    _device_body_cuts,
    _validate_devices,
)
from ._layout import load_layout, select_top_cells
from ._layout import region as _region
from ._layout import texts as _texts
from ._paths import _load_spec_json, _parse_layer_datatype, _validate_via_entries
from ._provenance import _content_hash, build_provenance
from .coverage import build_check_coverage, coverage_rollup, rollup_status, work_id
from .decks import ExtractionDeck, UnknownExtractionDeckError, deck_source_path
from .decks import get_extraction_deck as _get_extraction_deck
from .extract import (
    _capacitor_plate_regions,
    _capacitor_top_via_overlap_region,
    _resistor_body_region,
)

#: `1` -- unchanged since issue #859 (Phase 1a). Phase 1b (#860), Phase 1c
#: (#861), Phase 3 (#908), issue #1968, and issue #1979 all add fields/
#: optional spec keys additively -- no bump needed, per docs/cli/erc.md's
#: "Phase scope" and docs/json-contract.md's additive-envelope design:
#: `pdk`/`gates[].antenna_verdict`/`levels[].antenna_ratio`/
#: `levels[].antenna_ratio_max`/`levels[].antenna_ratio_source`/
#: `levels[].verdict` (Phase 1b), `erc_findings`/`erc_finding_count`
#: (Phase 1c), `levels[].remedy` (Phase 3), `status`/`provenance`
#: (issue #1968 -- the two fields `klt signoff` grading needs, see that
#: issue and docs/json-contract.md's "Shared `provenance` block"), and the
#: optional `stackup[0].active_layer` spec field (issue #1979 -- true
#: `poly ∩ diff` gate-area computation, opt-in, legacy raw-poly-area
#: behaviour preserved when omitted). Issue #2169 adds one more optional
#: spec key (`ties[].tap_requires`) and *fixes* the tie connectivity model
#: (a declared well no longer conducts across its plan-view extent, and
#: ties no longer share a graph with `gates[]`) -- a bug fix to what the
#: documented fields mean, not a change to the field set itself, so again
#: no bump: no consumer-visible field was added, removed, or retyped.
#: Issue #2179 adds `erc_status`/`erc_coverage` -- the connectivity half's
#: own roll-up and checked-work scope, beside the antenna-driven `status`/
#: `coverage` -- again additively: both new keys are pure additions, and no
#: existing field's value changes for any input (see `run_erc`).
#: Issue #2183 adds the optional `devices[]` spec section and the
#: `provenance.devices` echo of what it subtracted -- additive on both
#: sides: a spec that declares no `devices[]` produces a byte-identical
#: report except for the new (empty) `provenance.devices` list.
#: Issue #2199 adds the optional `ties[].tap_is_dedicated` spec key and
#: classifies a *degenerate* tie declaration as skipped rather than checked
#: work in `erc_coverage` (see `_degenerate_tie_reasons`). No field is added,
#: removed, or retyped: the change is which coverage list an existing
#: identity lands in, and the `erc_status` token that follows from it.
#: Issue #2204 adds the optional `--deck` CLI flag (no new spec key):
#: `provenance.deck` is populated (previously always `null`) and every
#: `provenance.devices[]` entry additionally carries `"source"`/
#: `"superseded_by"` -- but *only* when `--deck` is given. A run that omits
#: `--deck` (every caller before this issue) produces byte-identical output,
#: including `provenance.devices` entries with their pre-#2204 4-key shape
#: -- so, as with every prior additive change above, no bump.
#: Issue #2219 adds the opt-in `findings_only` mode (`--findings-only`) and
#: the top-level `findings_only` echo of it -- additive, and inert unless
#: the caller asks for it: every field of a run that does not pass the flag
#: is byte-identical to before. Inside such a run the *already nullable*
#: presentation is extended to `levels[].step_area_um2`/
#: `cumulative_area_um2`/`antenna_ratio` (null == "not measured", the same
#: thing `antenna_ratio_max: null` has always meant for the limit), which
#: is why it is an opt-in flag rather than a default: no existing caller's
#: report can acquire a null it did not ask for.
#: Issue #2234 adds two more optional spec keys, both additive and inert
#: unless used: ``ties[].tap_boxes`` (a caller-asserted list of tap-geometry
#: boxes, an alternative to ``tap_requires``/``tap_is_dedicated`` -- see
#: :func:`_extract_connectivity`) and top-level ``ties_disclosure`` (a
#: caller's explicit "no expressible tap, here is why" statement -- see
#: :func:`_validate_ties_disclosure`). A spec that gives neither produces
#: byte-identical output except for the new (``null``-when-absent)
#: ``ties_disclosure`` echo field and the new (empty-when-unused)
#: ``erc_coverage.checked_by_assertion`` list -- so, as with every prior
#: additive change above, no bump.
#: Issue #2247 adds one optional *sub*-key to that disclosure,
#: ``ties_disclosure.kind``, and one new
#: ``erc_coverage.inapplicable[].reason`` token it can record
#: (:data:`REASON_TIES_DISCLOSED_TOOL_LIMITATION`). Additive in the same
#: sense: the key defaults to the only meaning #2234 had
#: (:data:`DISCLOSURE_KIND_UNEXPRESSIBLE`), is echoed only when the spec
#: declared it, and the new reason token can only appear for a spec that
#: asked for it -- so every existing spec's report is byte-identical. No
#: bump.
#: Issue #2255 adds one more optional spec key (``ties[].well_boxes``),
#: widens an existing one (``ties[].well_layer`` accepts ``null`` when
#: ``well_boxes`` is given -- a *widening*, so no previously-valid spec
#: becomes invalid), adds one new
#: ``erc_coverage.skipped[].reason`` token it can record
#: (:data:`REASON_DEGENERATE_WELL_ASSERTION`), and adds one new
#: ``erc_coverage`` list beside #2234's ``checked_by_assertion``
#: (``checked_by_well_assertion``, empty unless used). Additive in exactly
#: the sense every entry above is: a spec that declares no ``well_boxes``
#: produces byte-identical output except for that new empty list. No bump.
#: Issue #2339 adds two more optional spec keys (``ties[].well_requires``
#: and ``ties[].well_excludes`` -- the well-side class selectors, see
#: :func:`_select_well_class`) and one new ``erc_coverage.skipped[].reason``
#: token they can record (:data:`REASON_DEGENERATE_WELL_SELECTION`). No new
#: output field and no new coverage list: a selection narrows *drawn*
#: geometry, so it is graded as an ordinary geometrically-derived pass
#: exactly as ``tap_requires`` is. A spec that declares neither key selects
#: nothing and produces byte-identical output, and the new reason token can
#: only appear for a spec that asked for it. No bump.
#: Issue #2400 adds one optional *sub*-key, ``nets[].islands`` (a declared
#: expected electrical-island count, integer >= 1, default 1 -- see
#: :func:`_validate_nets`). It changes only the grading of the
#: ``erc.unconnected_net`` count check for the spec entries that declare
#: it: declared N with N actual islands is clean (previously always a
#: finding for N > 1), declared N with any other actual count still
#: reports -- same rule id, same ``islands[]`` payload, with the
#: "expected exactly" description word carrying the declared count. A spec
#: that omits the key grades against 1 exactly as before, byte-identical,
#: and no output field is added, removed, or renamed -- so, as with every
#: prior additive change above, no bump.
SCHEMA_VERSION = 1


#: The stable ``coverage`` skip reason for every non-gate antenna level of a
#: ``findings_only`` run (issue #2219): the caller asked for the
#: ``erc_findings`` read only, so the per-gate accumulation those verdicts
#: are derived from was never performed. A *requested*-work skip in exactly
#: the sense :data:`REASON_MISSING_ANTENNA_PDK` already is -- both are the
#: caller declining to supply what the antenna half needs -- so it lands in
#: ``skipped`` rather than ``inapplicable``. See :func:`run_erc`.
REASON_FINDINGS_ONLY = "findings_only"

#: The skip reason for a level that could have been graded had ``--pdk``
#: named a PDK with an antenna-ratio table (or named one at all).
REASON_MISSING_ANTENNA_PDK = "missing_antenna_pdk"


def _antenna_skip_reason(pdk: str | None, findings_only: bool) -> str:
    """Why a non-gate ``levels[]`` entry came back ungraded, as the stable
    ``coverage.skipped[].reason`` token for it."""
    if findings_only:
        return REASON_FINDINGS_ONLY
    return REASON_MISSING_ANTENNA_PDK if pdk is None else "missing_antenna_limit"


def _antenna_coverage(
    gates: list[dict[str, Any]], pdk: str | None, findings_only: bool = False
) -> dict[str, Any]:
    """Grade coverage against actual non-gate antenna levels, not gate count."""
    checked = []
    skipped = []
    inapplicable = []
    reason = _antenna_skip_reason(pdk, findings_only)
    for gate in gates:
        for index, level in enumerate(gate["levels"]):
            identity = work_id("antenna", gate["gate_id"], level["layer"])
            if index == 0:
                inapplicable.append({"id": identity, "reason": "gate_reference_level"})
            elif level["verdict"] in {"pass", "violate"}:
                checked.append(identity)
            else:
                skipped.append({"id": identity, "reason": reason})
    return {
        "scope": "antenna",
        **build_check_coverage(
            checked=checked, skipped=skipped, inapplicable=inapplicable
        ),
    }


#: The stable ``erc_coverage`` skip reason for a ``ties[]`` entry whose
#: declared tap region is indistinguishable from ordinary source/drain
#: contacts (issue #2199). See :func:`_degenerate_tie_reasons` for exactly
#: when it applies and `docs/cli/erc.md` for how a spec clears it.
REASON_DEGENERATE_TAP_DECLARATION = "degenerate_tap_declaration"

#: The stable ``erc_coverage`` skip reason for a ``ties[]`` entry whose
#: caller-asserted substrate region (``well_layer: null`` + ``well_boxes``,
#: issue #2255) is geometrically indistinguishable from the whole top-cell
#: extent. The tap-side counterpart of
#: :data:`REASON_DEGENERATE_TAP_DECLARATION`, and kept distinct from it
#: because the two name different things to go fix: that one says "narrow
#: the tap", this one says "name the substrate region you are actually
#: claiming, not the die". See :func:`_degenerate_tie_reasons`.
REASON_DEGENERATE_WELL_ASSERTION = "degenerate_well_assertion"

#: The stable ``erc_coverage`` skip reason for a ``ties[]`` entry whose
#: declared well-side selection (``well_requires``/``well_excludes``, issue
#: #2339) did not actually select a *proper, non-empty* subset of the drawn
#: ``well_layer``: it either kept every merged shape on that layer (the
#: selection distinguishes nothing, so the entry silently grades the other
#: bias class against its own ``net`` exactly as an unselected tie would) or
#: kept none of them (the entry's per-well loop runs zero times, so zero
#: ``erc.missing_tie`` findings mean "nothing was examined", not "every well
#: is tied").
#:
#: Kept distinct from :data:`REASON_DEGENERATE_WELL_ASSERTION` because the
#: two say different things to go fix -- that one says "name the substrate
#: region you are actually claiming, not the die"; this one says "the marker
#: layer you named does not partition this well layer" -- and from
#: :data:`REASON_DEGENERATE_TAP_DECLARATION`, which is about the tap. Unlike
#: the tap test, this one fires only when a selection was *declared*: a tie
#: with no well-side selector claims every shape on the layer, which is the
#: strongest claim available and exactly what every spec written before this
#: key existed means. See :func:`_select_well_class`.
REASON_DEGENERATE_WELL_SELECTION = "degenerate_well_selection"

#: How much of the top cell's own extent a caller-asserted substrate region
#: (``ties[].well_boxes``, issue #2255) must leave **uncovered** to count as
#: a real claim rather than "the whole die is the substrate" -- as a
#: fraction of the top cell's bounding-box area.
#:
#: The bar is *indistinguishability*, not size: an assertion may legitimately
#: cover most of a block (a native substrate genuinely is everything outside
#: the n-wells), and nothing here requires it to be small. What it may not be
#: is the entire extent, because a single die-sized "well" polygon reduces
#: ``erc.missing_tie`` to "does any contact anywhere reach the declared
#: net?" -- satisfiable by every PMOS source on the rail, tap or not, which
#: is the same unfalsifiable pass issue #2199 rejected on the tap side. The
#: tolerance exists so a region drawn flush to the cell boundary, or one
#: rounded to the DBU grid, is not accepted on a technicality; it is
#: deliberately small, because the *evidence* an assertion carries grows
#: with how finely it partitions the block (each merged asserted polygon
#: must independently contain a tap that reaches the net).
_WELL_ASSERTION_MIN_UNCOVERED_FRACTION = 0.01

#: The stable ``erc_coverage.inapplicable`` reason for the undeclared
#: ``erc.missing_tie`` work when the spec's top-level ``ties_disclosure``
#: (issue #2234) is present -- in place of the default
#: :data:`REASON_NO_TIES_DECLARED` used when ``ties`` is simply omitted with
#: no disclosure at all. Both reasons describe the same "zero ties
#: declared" fact; only this one tells a reader the omission was a
#: considered, disclosed choice rather than an oversight -- see
#: :func:`_validate_ties_disclosure` and `docs/design-evidence-tiers.md`
#: item 11.
REASON_TIES_DISCLOSED_UNEXPRESSIBLE = "ties_disclosed_unexpressible"

#: The stable ``erc_coverage.inapplicable`` reason for the undeclared
#: ``erc.missing_tie`` work when the spec's top-level ``ties_disclosure``
#: declared ``"kind": "tool_limitation"`` (issue #2247) -- the *second*
#: disclosed shape, beside :data:`REASON_TIES_DISCLOSED_UNEXPRESSIBLE`.
#: Same "zero ties declared, deliberately, and here is why" fact; what
#: differs is **where the obstacle lives, and therefore what would clear
#: it**. An unexpressible stream has no tap to name (nothing drawn to
#: narrow, affirm, or bound) -- only different *geometry* clears it. A
#: tool-limitation stream can name its tap perfectly well, but declaring it
#: against the `klt` build the caller is pinned to produces a result they
#: have documented reason to distrust (the issue #2169 class of defect: a
#: build whose tie extraction is not isolated from the primary connectivity
#: graph turns a correct ``ties[]`` declaration into a false
#: ``erc.supply_short``) -- only a different *build* clears it. Collapsing
#: the two would tell a reader to go redraw a layout that is already
#: correct.
REASON_TIES_DISCLOSED_TOOL_LIMITATION = "ties_disclosed_tool_limitation"

#: The stable ``erc_coverage.inapplicable`` reason for the undeclared
#: ``erc.missing_tie`` work when ``ties`` is omitted/empty and no
#: ``ties_disclosure`` was given -- the pre-#2234 behaviour, named as a
#: constant so :data:`REASON_TIES_DISCLOSED_UNEXPRESSIBLE` has a documented
#: counterpart rather than a bare string literal to contrast with.
REASON_NO_TIES_DECLARED = "no_ties_declared"

#: ``ties_disclosure.kind`` (issue #2247): *why* this stream declares no
#: ``ties[]``. Default when the key is omitted, so every pre-#2247 spec
#: keeps the exact meaning it had.
DISCLOSURE_KIND_UNEXPRESSIBLE = "unexpressible"

#: ``ties_disclosure.kind`` (issue #2247): the tap is expressible, but the
#: `klt` build this evidence must be produced on cannot grade a declared tie
#: safely -- see :data:`REASON_TIES_DISCLOSED_TOOL_LIMITATION`.
DISCLOSURE_KIND_TOOL_LIMITATION = "tool_limitation"

#: Every accepted ``ties_disclosure.kind``, mapped to the
#: ``erc_coverage.inapplicable`` reason it records for the undeclared
#: ``erc.missing_tie`` work. A ``kind`` outside this table is a spec error
#: rather than a silent fallback to the default: a typo that quietly
#: downgraded a tool-limitation disclosure to an unexpressible one would
#: misdirect the reader of the report of record, which is the exact
#: conflation these reasons exist to end.
_TIES_DISCLOSURE_COVERAGE_REASON: dict[str, str] = {
    DISCLOSURE_KIND_UNEXPRESSIBLE: REASON_TIES_DISCLOSED_UNEXPRESSIBLE,
    DISCLOSURE_KIND_TOOL_LIMITATION: REASON_TIES_DISCLOSED_TOOL_LIMITATION,
}


def _ties_disclosure_reason(ties_disclosure: dict[str, str] | None) -> str:
    """The ``erc_coverage.inapplicable`` reason for undeclared
    ``erc.missing_tie`` work, given the spec's ``ties_disclosure`` (or its
    absence) -- see :func:`_connectivity_coverage`."""
    if not ties_disclosure:
        return REASON_NO_TIES_DECLARED
    return _TIES_DISCLOSURE_COVERAGE_REASON[
        ties_disclosure.get("kind", DISCLOSURE_KIND_UNEXPRESSIBLE)
    ]


def _connectivity_coverage(
    gates: list[dict[str, Any]],
    nets_decl: list[dict[str, Any]],
    ties: list[dict[str, Any]],
    degenerate_ties: dict[str, str] | None = None,
    asserted_ties: set[str] | None = None,
    ties_disclosure: dict[str, str] | None = None,
    well_asserted_ties: set[str] | None = None,
) -> dict[str, Any]:
    """The *second* checked-work scope this envelope carries (issue #2179):
    the connectivity/geometry rules behind ``erc_findings``.

    ``klt erc`` answers two independent questions in one envelope, and only
    one of them needs a PDK. The antenna half (:func:`_antenna_coverage`,
    ``scope: "antenna"``) grades nothing at all on a PDK whose antenna-ratio
    limits ``_ANTENNA_LIMITS_BY_PDK`` does not carry -- the degenerate case
    where *no* level can ever be graded, for every layout on that PDK. The
    connectivity half runs, and runs completely, with no ``--pdk``
    whatsoever, so it gets its own scope rather than being folded into the
    antenna one: a reader must be able to see "the connectivity rules ran
    and passed" without an antenna table existing, and equally must not be
    able to read a graded connectivity check as if it graded an antenna
    level.

    One identity per *subject actually checked*, matching the antenna
    scope's own "grade against real work, not a declaration count"
    discipline:

    - ``erc.floating_gate`` -- one per discovered gate. Always non-empty:
      ``run_erc`` raises before this point when no net carries gate-role
      geometry, and every gate has at least one level above the gate role
      (``stackup`` requires >= 2 entries), so the rule really is evaluated
      for each one.
    - ``erc.net_connectivity`` -- one per declared ``nets[]`` entry (the
      ``erc.unconnected_net``/``erc.multiply_driven_net``/
      ``erc.supply_short`` rules all key off the same declaration).
    - ``erc.missing_tie`` -- one per declared ``ties[]`` entry, *except*
      the degenerate ones (``degenerate_ties``, issue #2199): a tie whose
      declared tap region cannot be told apart from an ordinary
      source/drain contact is requested work that could not actually be
      performed, so it is recorded as **skipped** (the reason
      ``degenerate_ties`` carries for it:
      :data:`REASON_DEGENERATE_TAP_DECLARATION`, issue #2255's
      :data:`REASON_DEGENERATE_WELL_ASSERTION`, or issue #2339's
      :data:`REASON_DEGENERATE_WELL_SELECTION`) and the whole
      connectivity scope reads ``clean_partial`` rather than ``clean``.
      Without that, the one rule ``docs/design-evidence-tiers.md`` item 11
      requires to be zero can be satisfied by a declaration that never
      looked at a tap at all -- an unfalsifiable pass, indistinguishable in
      the envelope from a real one. A tie whose tap region was instead
      derived from a caller *assertion* (``tap_boxes``, issue #2234) rather
      than PDK-marker narrowing is, when not itself degenerate, both
      ``checked`` (it is real, evaluated work -- see :func:`_tie_findings`)
      *and* named again in the additional ``checked_by_assertion`` list this
      block carries (``asserted_ties``), so a consumer can tell it apart
      from a tie a bare ``tap_requires``/``tap_is_dedicated`` graded without
      re-reading the spec document.

    ``checked_by_well_assertion`` (``well_asserted_ties``, issue #2255) is
    the same idea applied one level down, and deliberately a **separate**
    list rather than more entries in ``checked_by_assertion``: there, the
    caller asserted which of the drawn ``tap_layer`` geometry counts as the
    tap, inside a well the layout actually draws; here, the caller asserted
    the **well region itself**, because the block sits in a native substrate
    that draws no well/tub layer at all. Those are different claims with
    different evidence value, and a reader grading the report of record must
    be able to tell which one was made. A tie can appear in both lists (an
    asserted substrate region whose tap is also named by ``tap_boxes``) or
    in either alone.

    A spec that declares no ``nets``/``ties`` asked for none of that work,
    so those rules are recorded as **inapplicable**, never skipped: a skip
    is requested work that did not run (and would make the scope partial),
    while an undeclared rule is work this invocation never asked for. That
    distinction is what lets a consumer tell "no supply was declared, so
    ``erc.supply_short`` was never computed" apart from "supplies were
    declared and came back clean" -- reading the envelope alone, without
    re-opening the spec document. When ``ties`` is empty *and* the spec
    carried a top-level ``ties_disclosure`` (issue #2234), the recorded
    reason is :data:`REASON_TIES_DISCLOSED_UNEXPRESSIBLE` instead of
    :data:`REASON_NO_TIES_DECLARED` -- same "zero declared" fact, but now
    distinguishable from an omission nobody considered -- or
    :data:`REASON_TIES_DISCLOSED_TOOL_LIMITATION` when that disclosure
    declared ``"kind": "tool_limitation"`` (issue #2247), which distinguishes
    "this stream has no tap to name" from "this stream has a tap, but the
    build that must produce this evidence cannot grade a declared tie
    safely". See :func:`_ties_disclosure_reason`.
    """
    degenerate = degenerate_ties or {}
    asserted = asserted_ties or set()
    well_asserted = well_asserted_ties or set()
    checked = [work_id("erc.floating_gate", gate["gate_id"]) for gate in gates]
    inapplicable: list[dict[str, str]] = []
    skipped: list[dict[str, str]] = []
    checked_by_assertion: list[str] = []
    checked_by_well_assertion: list[str] = []

    checked.extend(work_id("erc.net_connectivity", decl["name"]) for decl in nets_decl)
    if not nets_decl:
        inapplicable.append(
            {"id": work_id("erc.net_connectivity"), "reason": "no_nets_declared"}
        )

    for tie in ties:
        identity = work_id("erc.missing_tie", tie["name"])
        if tie["name"] in degenerate:
            skipped.append({"id": identity, "reason": degenerate[tie["name"]]})
        else:
            checked.append(identity)
            if tie["name"] in asserted:
                checked_by_assertion.append(identity)
            if tie["name"] in well_asserted:
                checked_by_well_assertion.append(identity)
    if not ties:
        inapplicable.append(
            {
                "id": work_id("erc.missing_tie"),
                "reason": _ties_disclosure_reason(ties_disclosure),
            }
        )

    return {
        "scope": "connectivity",
        **build_check_coverage(
            checked=checked, skipped=skipped, inapplicable=inapplicable
        ),
        "checked_by_assertion": sorted(checked_by_assertion),
        "checked_by_well_assertion": sorted(checked_by_well_assertion),
    }


class ErcError(Exception):
    """Raised when ``klt erc`` cannot run: a bad layout/spec file, a
    malformed stackup/via declaration, an unresolvable top cell, an unknown
    ``--pdk`` name, or a spec whose gate role matches no geometry at all.

    The CLI turns this into a clean stderr message + exit code 1, never a
    traceback -- see ``docs/json-contract.md``.
    """


#: Real sky130 antenna-ratio limits ("MAX_EGAR" -- the maximum effective-
#: area/gate-area ratio *without* an antenna diode), transcribed from the
#: official SkyWater sky130 antenna-rule table for the 5-metal "S8D" stack
#: option:
#: https://github.com/google/skywater-pdk/blob/main/docs/rules/antenna/table-Ia-antenna-rules-s8d.csv
#: -- each value is that table's own "Max EA/A w/o diode" column for the
#: named rule id (fetched 2026-08-12 via `gh search code` against
#: `google/skywater-pdk`, the canonical SkyWater sky130 PDK repository).
#:
#: Verified stack-invariant for every role below: the poly/licon1/li1/mcon/
#: met1/via1/met2 limits are identical across every sky130 metal-stack
#: option table checked (S8D, S8P/SP8P/S8P-10R, S8TM/S8TMC/S8TMA-5R,
#: S8P12/S8PIR/S8PF-10R, S8TNV-5R -- `docs/rules/antenna/table-I{a,e,c,g,b}-
#: antenna-rules-*.csv`), so this one table applies to every sky130 stack
#: variant, not just S8D specifically. met3-and-above limits *do* vary by
#: stack option (0.8-2.0 range across the checked tables) and are
#: intentionally not transcribed here -- a candidate follow-on, not a
#: silent omission (matching `decks/sky130.py`'s own convention of calling
#: out every deliberately-uncovered rule rather than pretending full
#: coverage).
#:
#: `licon1`/`mcon`/`via1` are kept here for citation completeness but are
#: never matched against a `stackup` role in practice: those are `klt erc`
#: *via* roles (see `_validate_vias`), not `stackup` roles, so they never
#: get a `levels[]` entry to attach a verdict to -- see this module's own
#: "Per gate" docstring section above. The source table also treats them
#: as area-only "Horizontal" checks (`<via-layer> area/gate area`, no
#: perimeter term) rather than the perimeter-based "Vertical" checks the
#: `stackup` roles use, a distinction this connectivity-area-only model
#: does not represent either way.
_SKY130_ANTENNA_RATIO_MAX_EGAR: dict[str, tuple[float, str]] = {
    "poly": (50.0, ".poly.1"),
    "licon1": (3.0, ".licon.1"),
    "li1": (75.0, ".li.1"),
    "mcon": (3.0, ".mcon.1"),
    "met1": (400.0, ".met1.1"),
    "via1": (6.0, ".via.1"),
    "met2": (400.0, ".met2.1"),
}

_SKY130_ANTENNA_SOURCE_URL = (
    "https://github.com/google/skywater-pdk/blob/main/docs/rules/antenna/"
    "table-Ia-antenna-rules-s8d.csv"
)

#: PDK name -> (role name -> (limit, source rule id)) lookup for `--pdk`.
#: sky130 only, per this repo's "open PDKs, sky130 first" scope (see
#: CLAUDE.md) -- gf180mcu antenna limits are a candidate follow-on, not
#: silently dropped.
_ANTENNA_LIMITS_BY_PDK: dict[str, dict[str, tuple[float, str]]] = {
    "sky130": _SKY130_ANTENNA_RATIO_MAX_EGAR,
}

_ANTENNA_SOURCE_URL_BY_PDK: dict[str, str] = {
    "sky130": _SKY130_ANTENNA_SOURCE_URL,
}


def _resolve_antenna_limits(pdk: str | None) -> dict[str, tuple[float, str]] | None:
    """Resolve ``--pdk`` to its antenna-ratio limit table.

    Returns ``None`` when ``pdk`` is ``None`` -- antenna ratios are still
    computed and reported on every level (see ``run_erc``), just with no
    PDK limit to compare against, so every level's ``verdict`` comes back
    ``"unchecked"``.

    Raises :class:`ErcError` for an unrecognised ``pdk`` name, matching
    ``klt drc``'s own ``--deck`` convention: an unknown name is a clean
    exit-1 error (not an argparse usage error), checked eagerly before any
    layout is even loaded.
    """
    if pdk is None:
        return None
    limits = _ANTENNA_LIMITS_BY_PDK.get(pdk)
    if limits is None:
        raise ErcError(
            f"unknown --pdk {pdk!r} (supported: "
            f"{', '.join(sorted(_ANTENNA_LIMITS_BY_PDK))})"
        )
    return limits


def _resolve_deck(deck: str | None) -> ExtractionDeck | None:
    """Resolve ``--deck`` to its curated :class:`~klayout_tools.decks.ExtractionDeck`
    (issue #2204) -- the deck-driven device-marker auto-detection this
    module's own ``devices[]`` carve-out (#2183) otherwise requires a caller
    to hand-transcribe.

    Deliberately the *same* curated, name-keyed registry lookup ``klt
    extract --deck``/``klt lvs --deck`` use
    (:func:`~klayout_tools.decks.get_extraction_deck`), not an installed PDK
    resolved via ``--pdk-root`` -- see this module's own docstring
    ("Device bodies", "Deck-driven device-marker auto-detection") for why
    that is enough: the curated deck already names every device-recognition
    marker layer a spec author would otherwise transcribe by hand, and
    needs no filesystem PDK install to read. Deliberately a *separate* flag
    from ``--pdk`` (which selects only the built-in antenna-ratio limit
    table, see :func:`_resolve_antenna_limits`) -- the two answer unrelated
    questions and ``--pdk`` has no ``gf180mcu`` entry today, while the
    extraction-deck registry does.

    Returns ``None`` when ``deck`` is ``None`` -- no auto-detection is
    attempted, and every device-related output (``gates[]``, every antenna
    ratio, every finding, ``provenance.deck``, ``provenance.devices``) is
    byte-identical to a run before this feature existed (issue #2204's own
    acceptance criteria).

    Raises :class:`ErcError` for an unrecognised deck name, matching
    :func:`_resolve_antenna_limits`'s own convention for an unrecognised
    ``--pdk``: a clean exit-1 error, not an argparse usage error, checked
    eagerly before any layout is even loaded.
    """
    if deck is None:
        return None
    try:
        return _get_extraction_deck(deck)
    except UnknownExtractionDeckError as exc:
        raise ErcError(str(exc)) from exc


def _validate_stackup(spec: dict[str, Any], spec_path: str) -> list[dict[str, Any]]:
    """Validate the ``stackup`` array: fabrication order from the gate
    layer up through every metal role a gate's net may reach.

    ``stackup[0]`` must declare ``"role": "gate"`` -- the polysilicon/gate
    layer that starts the accumulation. Every other entry is an ordinary
    metal role and must not repeat ``role: "gate"``. At least one metal
    role beyond the gate itself is required, or "layer-by-layer
    accumulation" has nothing to accumulate.

    ``stackup[0]`` may additionally set ``"active_layer"`` (issue #1979,
    optional, ``"<layer>/<datatype>"``, same shape as ``label_layer``): the
    diffusion/active layer used to compute *true* gate area as ``poly ∩
    diff`` rather than raw poly-net area -- see ``run_erc``'s gate-region
    computation and ``docs/cli/erc.md``'s "Spec file" section for the full
    rationale (a tie-cell/decap/filler-cell poly resistor has poly area but
    no gate oxide, since it never overlaps diffusion). Only meaningful on
    the gate role itself; rejected on any other entry.
    """
    raw = spec.get("stackup")
    if not isinstance(raw, list) or len(raw) < 2:
        raise ErcError(
            f"spec '{spec_path}' must have a 'stackup' array with at least "
            "two entries: the gate layer (stackup[0], role='gate') and at "
            "least one metal role above it"
        )

    entries: list[dict[str, Any]] = []
    names: list[str] = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ErcError(f"spec '{spec_path}': stackup[{i}] must be a JSON object")
        for key in ("name", "layer"):
            if key not in entry:
                raise ErcError(f"spec '{spec_path}': stackup[{i}] missing {key!r}")
        name = str(entry["name"])
        if name in names:
            raise ErcError(f"spec '{spec_path}': duplicate stackup name {name!r}")
        names.append(name)

        layer = _parse_layer_datatype(
            str(entry["layer"]), spec_path, f"stackup[{i}].layer", ErcError
        )
        label_layer = None
        if entry.get("label_layer") is not None:
            label_layer = _parse_layer_datatype(
                str(entry["label_layer"]),
                spec_path,
                f"stackup[{i}].label_layer",
                ErcError,
            )

        active_layer = None
        if entry.get("active_layer") is not None:
            if i != 0:
                raise ErcError(
                    f"spec '{spec_path}': stackup[{i}].active_layer is only "
                    "valid on stackup[0] (the gate role)"
                )
            active_layer = _parse_layer_datatype(
                str(entry["active_layer"]),
                spec_path,
                f"stackup[{i}].active_layer",
                ErcError,
            )

        role = entry.get("role")
        if role is not None and role != "gate":
            raise ErcError(
                f"spec '{spec_path}': stackup[{i}].role must be omitted or "
                f"'gate' (got {role!r})"
            )
        if i == 0 and role != "gate":
            raise ErcError(
                f'spec \'{spec_path}\': stackup[0] must set "role": "gate" '
                "-- the polysilicon/gate layer, first in fabrication order"
            )
        if i > 0 and role == "gate":
            raise ErcError(
                f"spec '{spec_path}': only stackup[0] may set \"role\": "
                f'"gate" (a second one was found at stackup[{i}])'
            )

        entries.append(
            {
                "name": name,
                "layer": layer,
                "label_layer": label_layer,
                "active_layer": active_layer,
                "role": role,
            }
        )

    return entries


def _validate_vias(
    spec: dict[str, Any], spec_path: str, stackup_names: list[str]
) -> list[dict[str, Any]]:
    return [
        {"name": name, "layer": layer, "between": between}
        for _, _, name, layer, between in _validate_via_entries(
            spec, spec_path, stackup_names, ErcError
        )
    ]


def _validate_nets(spec: dict[str, Any], spec_path: str) -> list[dict[str, Any]]:
    """Validate the optional ``nets`` array (issue #861): named nets to
    check for connectivity findings (``erc.unconnected_net``,
    ``erc.multiply_driven_net``, ``erc.supply_short``). Mirrors ``klt
    power``'s ``power_nets`` name-matching convention, plus a ``kind`` used
    to classify a two-net short as a plain multiply-driven net vs. a supply
    short, and (issue #2400) an ``islands`` expected electrical-island
    count the ``erc.unconnected_net`` check grades against instead of the
    implicit 1 -- see :func:`_net_connectivity_findings` for why a
    declared count is the falsifiable form. Omitted or empty -> no
    net-connectivity findings are computed
    (a caller who only wants the floating-gate check need not declare
    this)."""
    raw = spec.get("nets", [])
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        raise ErcError(f"spec '{spec_path}': 'nets' must be an array")

    entries: list[dict[str, Any]] = []
    names: list[str] = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ErcError(f"spec '{spec_path}': nets[{i}] must be a JSON object")
        if "name" not in entry:
            raise ErcError(f"spec '{spec_path}': nets[{i}] missing 'name'")
        name = str(entry["name"]).strip()
        if not name:
            raise ErcError(f"spec '{spec_path}': nets[{i}].name must be non-empty")
        if name in names:
            raise ErcError(f"spec '{spec_path}': duplicate net name {name!r}")
        names.append(name)

        kind = entry.get("kind", "signal")
        if kind not in ("signal", "supply"):
            raise ErcError(
                f"spec '{spec_path}': nets[{i}].kind must be 'signal' or "
                f"'supply' (got {kind!r})"
            )

        # Issue #2400: the declared expected island count. Default 1 is the
        # pre-#2400 model (one label string == one electrical net); a
        # stream whose library PG pin names legitimately land on N separate
        # domains declares N so `erc.unconnected_net` grades the count
        # instead of silently failing every such name. ``islands: 1`` must
        # behave exactly like the omitted form.
        islands = entry.get("islands", 1)
        if isinstance(islands, bool) or not isinstance(islands, int) or islands < 1:
            raise ErcError(
                f"spec '{spec_path}': nets[{i}].islands must be an integer "
                f">= 1 (got {entry.get('islands')!r})"
            )

        entries.append({"name": name, "kind": kind, "islands": islands})
    return entries


def _parse_tie_layer_list(
    entry: dict[str, Any], key: str, spec_path: str, index: int
) -> list[tuple[int, int]]:
    """One ``ties[]`` entry's ``["<layer>/<datatype>", ...]`` list under
    ``key`` -- the shared parser behind ``tap_requires`` (issue #2169,
    :func:`_parse_tap_requires`) and the well-side ``well_requires`` /
    ``well_excludes`` (issue #2339, :func:`_parse_well_selection`), which
    take the identical plain-layer-list shape and differ only in what the
    layers *mean*. The layer-list counterpart of :func:`_parse_um_boxes`.

    Omitted/``null`` -> ``[]``. Every entry is validated by
    :func:`~klayout_tools._paths._parse_layer_datatype` rather than coerced:
    a silently-dropped layer would weaken a narrowing the caller believes
    they declared.
    """
    raw = entry.get(key, [])
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        raise ErcError(
            f"spec '{spec_path}': ties[{index}].{key} must be an array "
            "of '<layer>/<datatype>' strings"
        )
    return [
        _parse_layer_datatype(
            str(value), spec_path, f"ties[{index}].{key}[{j}]", ErcError
        )
        for j, value in enumerate(raw)
    ]


def _parse_tap_requires(
    entry: dict[str, Any], spec_path: str, index: int
) -> list[tuple[int, int]]:
    """``ties[].tap_requires`` (optional, issue #2169): the extra layers
    intersected into this tie's ``tap_layer`` to derive the real tap --
    ``{"tap_layer": "22/0", "tap_requires": ["32/0"]}`` is ``Comp ∩
    Nplus``. Omitted/``null`` -> ``[]`` (``tap_layer`` alone, the
    pre-#2169 behaviour). Split out of :func:`_validate_ties` to keep that
    function under the repo's C901 complexity ratchet."""
    return _parse_tie_layer_list(entry, "tap_requires", spec_path, index)


def _parse_tap_is_dedicated(entry: dict[str, Any], spec_path: str, index: int) -> bool:
    """``ties[].tap_is_dedicated`` (optional, issue #2199): the spec's
    affirmation that ``tap_layer`` names a layer drawn **only** for taps --
    sky130's own ``tap`` (65/44), a PDK tub-contact marker -- so no
    ``tap_requires`` narrowing is needed for the derived tap to be a real
    tap.

    It is not a hint and it changes no geometry: it is the one declarative
    way to say "there is nothing to narrow here", which is what keeps such
    a tie *checked* work instead of the skipped-degenerate classification
    :func:`_degenerate_tie_reasons` otherwise assigns. Omitted/``null`` ->
    ``False``. Anything but a JSON boolean is rejected rather than coerced:
    a truthy ``"false"`` string quietly asserting the opposite of what it
    reads is exactly the silent-pass shape issue #2199 is about.

    Split out of :func:`_validate_ties` to keep that function under the
    repo's C901 complexity ratchet, as :func:`_parse_tap_requires` is.
    """
    raw = entry.get("tap_is_dedicated", False)
    if raw is None:
        return False
    if not isinstance(raw, bool):
        raise ErcError(
            f"spec '{spec_path}': ties[{index}].tap_is_dedicated must be true or false"
        )
    return raw


def _parse_um_boxes(
    entry: dict[str, Any], key: str, spec_path: str, index: int
) -> list[tuple[float, float, float, float]]:
    """One ``ties[]`` entry's ``[left, bottom, right, top]`` micrometre box
    list under ``key`` -- the shared parser behind ``tap_boxes`` (issue
    #2234, :func:`_parse_tap_boxes`) and ``well_boxes`` (issue #2255,
    :func:`_parse_well_boxes`), which take the identical literal-geometry
    shape and differ only in what the boxes *mean*.

    ``[left, bottom, right, top]`` is the same tuple
    :func:`~klayout_tools._layout.clip_box` already converts for ``klt
    clip``/``components.py``. Omitted/``null`` -> ``[]``. Every box is
    validated rather than coerced: a 3-element array, a non-number, a bool
    (JSON ``true`` is not a coordinate), or an inverted/degenerate extent is
    a spec error, since a silently-dropped box would weaken an assertion the
    caller believes they made.
    """
    raw = entry.get(key, [])
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        raise ErcError(
            f"spec '{spec_path}': ties[{index}].{key} must be an array of "
            "[left, bottom, right, top] micrometre boxes"
        )
    boxes: list[tuple[float, float, float, float]] = []
    for j, value in enumerate(raw):
        field = f"ties[{index}].{key}[{j}]"
        if (
            not isinstance(value, list)
            or len(value) != 4
            or not all(
                isinstance(v, (int, float)) and not isinstance(v, bool) for v in value
            )
        ):
            raise ErcError(
                f"spec '{spec_path}': {field} must be a 4-number "
                "[left, bottom, right, top] array"
            )
        left, bottom, right, top = (float(v) for v in value)
        if left >= right or bottom >= top:
            raise ErcError(
                f"spec '{spec_path}': {field} must have left < right and bottom < top"
            )
        boxes.append((left, bottom, right, top))
    return boxes


def _parse_tap_boxes(
    entry: dict[str, Any], spec_path: str, index: int
) -> list[tuple[float, float, float, float]]:
    """``ties[].tap_boxes`` (optional, issue #2234): a caller *assertion* of
    where the tap geometry is, as a list of ``[left, bottom, right, top]``
    micrometre boxes intersected into ``tap_layer``. Composes with
    ``tap_requires`` (both narrow the same region) but, unlike it, needs no
    PDK marker layer to exist at all: a stream whose taps are genuinely
    drawn but carry no distinguishing implant/marker layer can point
    directly at them instead. Omitted/``null`` -> ``[]`` (no assertion,
    pre-#2234 behaviour, byte-identical). Split out of :func:`_validate_ties`
    to keep that function under the repo's C901 complexity ratchet, as
    :func:`_parse_tap_requires` is.
    """
    return _parse_um_boxes(entry, "tap_boxes", spec_path, index)


def _parse_well_boxes(
    entry: dict[str, Any], spec_path: str, index: int
) -> list[tuple[float, float, float, float]]:
    """``ties[].well_boxes`` (optional, issue #2255): a caller *assertion*
    of the substrate/well region this tie is about, for a block that draws
    no well/tub layer for it to name -- the same ``[left, bottom, right,
    top]`` micrometre box list ``tap_boxes`` takes, and deliberately so, but
    a fundamentally different kind of claim.

    ``tap_boxes`` **narrows** a required, drawn ``tap_layer``: it composes
    with the geometry, and the geometry is still what is measured. This
    **substitutes** for a required drawn layer, so the region it names is
    not corroborated by anything in the stream at all. That is why it is
    valid only with ``well_layer: null`` (:func:`_parse_well_declaration`),
    why it is graded under its own coverage classification rather than
    folded into ``checked_by_assertion``, and why it carries its own
    degeneracy test (:func:`_degenerate_tie_reasons`).
    """
    return _parse_um_boxes(entry, "well_boxes", spec_path, index)


def _parse_well_selection(
    entry: dict[str, Any], spec_path: str, index: int, drawn_well: bool
) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    """``ties[].well_requires`` / ``ties[].well_excludes`` (optional, issue
    #2339): the marker layers that pick out *which* merged shapes of a drawn
    ``well_layer`` this tie is about -- kept when the shape interacts with
    geometry on every ``well_requires`` layer and with none of the
    ``well_excludes`` layers.

    The well-side counterpart of ``tap_requires``, for the one thing the tap
    side cannot express: a single drawn tub layer carrying two
    differently-biased well classes (device-body wells on one rail, a
    vertical bipolar's base tub on the other). Each class is declared as its
    own ``ties[]`` entry with its own ``net``, selected by a PDK device
    marker -- one entry naming it under ``well_requires``, the complementary
    entry naming the same marker under ``well_excludes``.

    Both require a **drawn** ``well_layer``: there is nothing to select from
    on a tie whose well region is a caller assertion (``well_layer: null`` +
    ``well_boxes``, issue #2255) -- the asserted boxes already *are* exactly
    the region the caller means, box by box, so a second layer-driven
    narrowing on top of them would be an unstated claim about geometry that
    corroborates nothing. Rejected rather than silently ignored, for the
    same reason :func:`_parse_um_boxes` rejects a malformed box.

    Split out of :func:`_parse_well_declaration` to keep that function
    under the repo's C901 complexity ratchet, as
    :func:`_parse_tap_requires` is split out of :func:`_validate_ties`.
    """
    requires = _parse_tie_layer_list(entry, "well_requires", spec_path, index)
    excludes = _parse_tie_layer_list(entry, "well_excludes", spec_path, index)
    if not drawn_well and (requires or excludes):
        named = "well_requires" if requires else "well_excludes"
        raise ErcError(
            f"spec '{spec_path}': ties[{index}].{named} selects among the "
            f"drawn shapes of a well layer, so ties[{index}].well_layer must "
            "name one (got null); an asserted substrate region already names "
            "exactly the region it claims, box by box, in 'well_boxes'"
        )
    return requires, excludes


def _parse_well_declaration(
    entry: dict[str, Any], spec_path: str, index: int
) -> dict[str, Any]:
    """One ``ties[]`` entry's well declaration -- the ``well_layer`` /
    ``well_boxes`` / ``well_requires`` / ``well_excludes`` keys, of which
    exactly one of the first two is populated (issue #2255).

    Two mutually exclusive forms, because they are different evidence and
    conflating them would let an assertion hide behind drawn geometry:

    - ``"well_layer": "<layer>/<datatype>"`` (every spec before #2255) --
      the drawn well/tub layer, each of whose merged shapes is checked
      independently. ``well_boxes`` alongside it is rejected rather than
      silently applied: there is nothing this module could do with a box
      list over a drawn well that is not either a narrowing ``tap_boxes``
      already expresses better, or a second, unstated claim.
    - ``"well_layer": null`` + a non-empty ``well_boxes`` -- the
      native-substrate form: nothing is drawn, so the caller names the
      region they are claiming.

    ``well_requires``/``well_excludes`` (issue #2339,
    :func:`_parse_well_selection`) ride on the *drawn* form only: they pick
    which of that layer's merged shapes this tie is about, so a single tub
    layer holding two differently-biased well classes can be declared as
    two complementary entries instead of one entry that reports every well
    of the other class as untied.

    ``well_layer: null`` with no ``well_boxes`` is an error, not a tie that
    quietly checks nothing: "there is no well to name and I am not asserting
    one" is *exactly* the state a spec already expresses by omitting the
    ``ties[]`` entry and disclosing it with ``ties_disclosure`` -- and
    unlike this, that route cannot be mistaken for a graded check. The key
    itself stays required (an entry that omits ``well_layer`` altogether
    still raises the same "missing 'well_layer'" error it always has): the
    absence of a drawn well must be *declared*, never inferred from a typo.

    Split out of :func:`_validate_ties` to keep that function under the
    repo's C901 complexity ratchet, as :func:`_parse_tap_requires` is.
    """
    well_boxes = _parse_well_boxes(entry, spec_path, index)
    raw = entry["well_layer"]
    well_requires, well_excludes = _parse_well_selection(
        entry, spec_path, index, raw is not None
    )
    declaration: dict[str, Any] = {
        "well_boxes": well_boxes,
        "well_requires": well_requires,
        "well_excludes": well_excludes,
    }
    if raw is None:
        if not well_boxes:
            raise ErcError(
                f"spec '{spec_path}': ties[{index}].well_layer is null, so "
                f"ties[{index}].well_boxes must assert the substrate region "
                "this tie covers (a non-empty array of [left, bottom, right, "
                "top] micrometre boxes); to declare no tie at all, omit the "
                "entry and state why in the top-level 'ties_disclosure'"
            )
        return {"well_layer": None, **declaration}
    if well_boxes:
        raise ErcError(
            f"spec '{spec_path}': ties[{index}].well_boxes asserts a substrate "
            f"region for a tie with no drawn well, so ties[{index}].well_layer "
            f"must be null (got {str(raw)!r}); to narrow a drawn well's taps, "
            "use 'tap_boxes', and to select which of its shapes this tie is "
            "about, 'well_requires'/'well_excludes'"
        )
    return {
        "well_layer": _parse_layer_datatype(
            str(raw), spec_path, f"ties[{index}].well_layer", ErcError
        ),
        **declaration,
    }


def _validate_ties(
    spec: dict[str, Any], spec_path: str, stackup_names: list[str]
) -> list[dict[str, Any]]:
    """Validate the optional ``ties`` array (issue #861): substrate/well
    tie declarations for the ``erc.missing_tie`` check. Each entry names a
    well/tub layer and the tap (contact) layer expected inside it, the
    ``stackup`` role the tap must be wired up to, and the supply net that
    tap must ultimately reach. Omitted or empty -> no missing-tie findings
    are computed.

    ``tap_requires`` (optional array of ``"<layer>/<datatype>"``, issue
    #2169) intersects further layers into the tap, so the spec can express
    the *boolean* a real PDK tap is drawn as -- ``Comp ∩ Nplus`` inside an
    n-well, ``Comp ∩ Pplus`` inside a p-well -- rather than a single layer
    that is either an implant (not a conductor) or a diffusion/cut shared
    with every source/drain in the same well. Same "second plain layer
    field, intersected at use time" shape as ``stackup[0].active_layer``
    (issue #1979), generalised to a list; omitted -> ``tap_layer`` alone,
    unchanged.

    ``tap_is_dedicated`` (optional boolean, issue #2199) is the other way
    to say the same thing for a PDK that draws taps on their own layer:
    nothing needs narrowing because the layer is already tap-only. A tie
    that declares neither is *degenerate* -- see
    :func:`_degenerate_tie_reasons` -- and is graded as skipped rather than
    checked work.

    ``tap_boxes`` (optional array of ``[left, bottom, right, top]``
    micrometre boxes, issue #2234) is a third, caller-*asserted* way: rather
    than narrowing ``tap_layer`` by a PDK marker layer's boolean, the spec
    names the tap geometry directly. For a stream that draws taps with no
    distinguishing marker at all -- neither ``tap_requires`` nor
    ``tap_is_dedicated`` has anything true to narrow or affirm -- this is
    the only way to declare a tie that is not degenerate. Graded under its
    own coverage classification (:func:`_connectivity_coverage`'s
    ``checked_by_assertion``) rather than folded into an ordinary
    geometrically-derived pass, and held to the same falsifiability test as
    every other narrowing form (:func:`_degenerate_tie_reasons`): an
    assertion that removes nothing from the drawn ``tap_layer`` is still
    degenerate.

    ``well_boxes`` (optional array of the same micrometre boxes, issue
    #2255) is the *well*-side counterpart, and the one thing on this entry
    that substitutes for a required field rather than narrowing one: with
    ``"well_layer": null``, it asserts the substrate region of a block that
    draws no well/tub layer at all (a native-substrate, NMOS-in-bulk
    block), which before this could not declare a substrate tie in any
    form. Exactly one of the two well forms may be given -- see
    :func:`_parse_well_declaration` -- and an assertion covering the whole
    top-cell extent is graded as skipped work, not as evidence (see
    :func:`_degenerate_tie_reasons`).

    ``well_requires``/``well_excludes`` (optional arrays of
    ``"<layer>/<datatype>"``, issue #2339) narrow the *drawn* well the way
    ``tap_requires`` narrows the tap, but by **selecting whole merged
    shapes** of ``well_layer`` rather than intersecting it: a shape is kept
    when it interacts with geometry on every ``well_requires`` layer and
    with none of the ``well_excludes`` layers. That is what lets one tub
    layer holding two differently-biased well classes be declared as two
    complementary entries, each with its own ``net``, instead of one entry
    that reports every correctly-tapped well of the other class as
    ``erc.missing_tie``. Valid only with a drawn ``well_layer`` (see
    :func:`_parse_well_selection`), and held to the same falsifiability bar
    as every other narrowing here: a selection keeping every drawn shape, or
    none of them, is graded as skipped work (see
    :func:`_degenerate_tie_reasons`)."""
    raw = spec.get("ties", [])
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        raise ErcError(f"spec '{spec_path}': 'ties' must be an array")

    entries: list[dict[str, Any]] = []
    names: list[str] = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ErcError(f"spec '{spec_path}': ties[{i}] must be a JSON object")
        for key in ("well_layer", "tap_layer", "connect_to", "net"):
            if key not in entry:
                raise ErcError(f"spec '{spec_path}': ties[{i}] missing {key!r}")

        name = str(entry.get("name", f"tie{i}"))
        if name in names:
            raise ErcError(f"spec '{spec_path}': duplicate tie name {name!r}")
        names.append(name)

        well = _parse_well_declaration(entry, spec_path, i)
        tap_layer = _parse_layer_datatype(
            str(entry["tap_layer"]), spec_path, f"ties[{i}].tap_layer", ErcError
        )

        tap_requires = _parse_tap_requires(entry, spec_path, i)
        tap_is_dedicated = _parse_tap_is_dedicated(entry, spec_path, i)
        tap_boxes = _parse_tap_boxes(entry, spec_path, i)

        connect_to = str(entry["connect_to"])
        if connect_to not in stackup_names:
            raise ErcError(
                f"spec '{spec_path}': ties[{i}].connect_to must name a "
                f"'stackup' entry (got {connect_to!r})"
            )

        net = str(entry["net"]).strip()
        if not net:
            raise ErcError(f"spec '{spec_path}': ties[{i}].net must be non-empty")

        entries.append(
            {
                "name": name,
                **well,
                "tap_layer": tap_layer,
                "tap_requires": tap_requires,
                "tap_is_dedicated": tap_is_dedicated,
                "tap_boxes": tap_boxes,
                "connect_to": connect_to,
                "net": net,
            }
        )
    return entries


def _validate_ties_disclosure(
    spec: dict[str, Any], spec_path: str
) -> dict[str, str] | None:
    """The optional top-level ``ties_disclosure`` spec key (issue #2234): a
    caller's explicit statement that this run declares no tap, and why --
    ``{"reason": "<non-empty string>", "kind": "<kind>"}``. Omitted/``null``
    -> ``None`` (pre-#2234 behaviour, unchanged).

    This changes no geometry and no finding: :func:`run_erc` computes
    exactly the same ``erc_findings`` whether or not it is given. What it
    changes is the ``erc_coverage.inapplicable`` reason recorded for the
    undeclared ``erc.missing_tie`` work when ``ties`` is empty
    (:func:`_ties_disclosure_reason`, in place of
    :data:`REASON_NO_TIES_DECLARED`, see :func:`_connectivity_coverage`), so
    a consumer -- `klt signoff`'s T1 item 11 in particular, see
    `docs/design-evidence-tiers.md` -- can distinguish a considered,
    disclosed omission from "nobody declared ties", which previously
    rendered identically. A non-empty ``ties`` declaration alongside a
    disclosure is not rejected (a spec may express some taps and disclose
    the rest), but the disclosure only ever affects the *undeclared* work's
    reason.

    ``kind`` (optional, issue #2247) says which obstacle is being disclosed,
    because the two have different remedies and a reader of the report of
    record should not have to guess which one to go fix:

    - :data:`DISCLOSURE_KIND_UNEXPRESSIBLE` (the default, and the only
      meaning #2234 had) -- there is no tap to name: no narrowing marker, no
      dedicated tap layer, no nameable tap geometry. Cleared by drawing
      something, not by a newer `klt`.
    - :data:`DISCLOSURE_KIND_TOOL_LIMITATION` -- the tap *is* expressible,
      but on the `klt` build this evidence has to be produced with, a
      declared tie is not safely gradeable (issue #2169's unisolated tie
      extraction turning a correct ``ties[]`` into a false
      ``erc.supply_short`` is the reported instance). Cleared by a different
      build, not by different geometry.

    **Neither kind is verified by this run, and neither can be.** A
    disclosure is the caller's word about their own stream or their own
    toolchain; that is exactly why it never moves a finding and why item 11
    stays ``"unmet"`` on both. What the report *does* pin down is the build
    that produced it -- ``provenance.klt_version`` -- so a reader can check
    for themselves whether a disclosed tool limitation applies to the run in
    front of them.

    Validated the same strict way every other optional boolean/string spec
    key in this module is (:func:`_parse_tap_is_dedicated`): present but
    malformed is a spec error, not silently coerced or ignored -- an empty
    or missing ``reason``, or a ``kind`` outside the two above, would be
    exactly the unfalsifiable "disclosed nothing" shape this key exists to
    rule out. ``kind`` is echoed only when the spec actually declared it, so
    every pre-#2247 spec's report stays byte-identical.
    """
    raw = spec.get("ties_disclosure")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ErcError(f"spec '{spec_path}': 'ties_disclosure' must be a JSON object")
    reason = raw.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise ErcError(
            f"spec '{spec_path}': ties_disclosure.reason must be a non-empty string"
        )
    disclosure = {"reason": reason.strip()}
    if "kind" in raw and raw["kind"] is not None:
        kind = raw["kind"]
        if not isinstance(kind, str) or kind not in _TIES_DISCLOSURE_COVERAGE_REASON:
            allowed = ", ".join(sorted(_TIES_DISCLOSURE_COVERAGE_REASON))
            raise ErcError(
                f"spec '{spec_path}': ties_disclosure.kind must be one of "
                f"{allowed} (got {kind!r})"
            )
        disclosure["kind"] = kind
    return disclosure


def _deck_device_cuts(
    layout: Any,
    top_cell: Any,
    deck: ExtractionDeck,
    role_layers: dict[str, tuple[int, int]],
    declared_roles: dict[str, str],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Resolve a curated extraction ``deck``'s own device-marker
    declarations into ``(cuts, applied)`` -- the deck-driven analogue of
    :func:`klayout_tools._devices._device_body_cuts` (issue #2204, built on
    #2183's hand-declared ``devices[]``).

    **The mapping rule** (this issue's own "Marker-to-role mapping" design
    note): a deck device applies to a declared ``stackup``/``vias`` role
    *only when the device's conducting-body layer equals that role's
    ``(layer, datatype)`` exactly* -- no name-guessing, no partial-overlap
    heuristics. The subtracted region is computed the *same* way ``klt
    extract``'s own device recognition computes it, factored out of
    ``extract.py`` for exactly this reuse (issue #2204):

    - :class:`~klayout_tools.decks.ResistorDevice` -- conducting-body layer
      is ``body``; region is ``body & marker`` narrowed by ``requires``/
      ``excludes`` (:func:`~klayout_tools.extract._resistor_body_region`).
    - :class:`~klayout_tools.decks.CapacitorDevice` -- two independent
      conducting-body layers, each checked separately:

      - ``top_plate`` -- region is the recognised top-plate region itself
        (:func:`~klayout_tools.extract._capacitor_plate_regions`'s first
        return value), narrowed by ``top_plate_requires``/
        ``top_plate_excludes``.
      - ``top_plate_via`` (when the deck declares one) -- region is *only*
        the geometric overlap between that via's own footprint and this
        capacitor's recognised bottom plate
        (:func:`~klayout_tools.extract._capacitor_top_via_overlap_region`,
        issue #364/#1388's own derivation) -- **not** the whole via layer,
        which is typically also this deck's ordinary inter-metal via role
        (e.g. gf180mcu's Via4 both lands a MiM cap's top plate on Metal5
        *and* routes ordinary Metal4-Metal5 vias everywhere else): cutting
        the entire layer from a matching ``vias`` role would silently
        disconnect every legitimate via on it, not just the ones under a
        capacitor.
      - ``bottom_plate`` is deliberately **not** a matched layer: unlike a
        resistor body or a MiM top plate, a capacitor's bottom plate is
        ordinary conductor that genuinely carries the same net's real
        routing (``klt extract`` ties it into the metal's own connectivity
        node rather than cutting it out) -- subtracting it here would
        introduce a false disconnect, not fix one.

    Only `~klayout_tools.decks.ResistorDevice`/`CapacitorDevice` entries are
    matched -- `BipolarDevice`/`DiodeDevice`/`MomCapacitorDevice` are a
    candidate follow-on, not a silent omission (matching this repo's own
    "name every deliberately-uncovered case" convention).

    **A device whose conducting-body layer matches no declared role is
    still listed** (``on: None``, ``body_area_um2: 0.0``) whenever that
    layer actually carries geometry on this layout -- so a spec that
    declares the wrong role name, or omits the role a real device sits on
    entirely, is visible rather than silently invisible (issue #2204's own
    acceptance criterion). A device whose layer carries *no* geometry at
    all here is omitted outright: every curated deck carries dozens of
    resistor/capacitor flavours a given design never draws, and listing
    every one of them on every ``--deck``-selected run would bury the
    signal this criterion exists to surface.

    **An explicit ``devices[]`` entry wins** (this issue's own acceptance
    criterion): ``declared_roles`` (role name -> the hand-declared device
    name that already covers it, built by the caller from the *validated*
    ``devices[]`` list) marks a role as already spoken for. A deck device
    that would otherwise apply there is still listed, with
    ``body_area_um2: 0.0`` and ``superseded_by`` naming the declared entry
    that won -- so the precedence is visible in the report, not just
    implied by its absence from ``cuts``.

    Every entry additionally carries ``"source": "deck"``, distinguishing
    it from a hand-declared entry's own ``"source": "declared"`` (added by
    ``run_erc`` only when a deck is selected -- see that function for why
    the field is conditional).
    """
    dbu2_um2 = layout.dbu * layout.dbu
    roles_by_layer: dict[tuple[int, int], list[str]] = {}
    for role, layer in role_layers.items():
        roles_by_layer.setdefault(layer, []).append(role)

    cuts: dict[str, Any] = {}
    applied: list[dict[str, Any]] = []

    def _record(name: str, layer: tuple[int, int], region: Any) -> None:
        region = region.merged()
        if region.is_empty():
            return
        layer_str = f"{layer[0]}/{layer[1]}"
        matched_roles = roles_by_layer.get(layer, [])
        if not matched_roles:
            applied.append(
                {
                    "name": name,
                    "body_layer": layer_str,
                    "on": None,
                    "body_area_um2": 0.0,
                    "source": "deck",
                    "superseded_by": None,
                }
            )
            return
        for role in matched_roles:
            declared_name = declared_roles.get(role)
            if declared_name is not None:
                applied.append(
                    {
                        "name": name,
                        "body_layer": layer_str,
                        "on": role,
                        "body_area_um2": 0.0,
                        "source": "deck",
                        "superseded_by": declared_name,
                    }
                )
                continue
            cuts[role] = (cuts[role] + region).merged() if role in cuts else region
            applied.append(
                {
                    "name": name,
                    "body_layer": layer_str,
                    "on": role,
                    "body_area_um2": round(region.area() * dbu2_um2, 9),
                    "source": "deck",
                    "superseded_by": None,
                }
            )

    for resistor in deck.resistors:
        _record(
            resistor.name,
            resistor.body,
            _resistor_body_region(layout, top_cell, resistor),
        )

    for capacitor in deck.capacitors:
        top_region, _bottom_region = _capacitor_plate_regions(
            layout, top_cell, capacitor
        )
        _record(capacitor.name, capacitor.top_plate, top_region)
        if capacitor.top_plate_via is not None:
            _record(
                capacitor.name,
                capacitor.top_plate_via,
                _capacitor_top_via_overlap_region(layout, top_cell, capacitor),
            )

    return cuts, applied


def _bbox_dict(box: Any) -> dict[str, int]:
    """A violation-style ``bbox`` dict (raw database units, matching
    ``klt drc``'s own ``violations[].bbox`` convention -- see
    ``drc.py``'s ``run_drc`` docstring)."""
    return {"left": box.left, "bottom": box.bottom, "right": box.right, "top": box.top}


def _finding(
    rule: str,
    description: str,
    *,
    net: str | None = None,
    other_net: str | None = None,
    gate_id: str | None = None,
    layer: str | None = None,
    bbox: dict[str, int] | None = None,
    islands: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """One ``erc_findings[]`` entry (issue #861's finding shape, see this
    module's docstring "ERC finding checks"): the 8-key dict shared
    verbatim by every rule id (``erc.floating_gate``,
    ``erc.unconnected_net``, ``erc.multiply_driven_net``,
    ``erc.supply_short``, ``erc.missing_tie``) -- only which of
    ``net``/``other_net``/``gate_id``/``layer``/``bbox``/``islands`` are
    populated vs. left ``None`` varies per call site. Mirrors
    ``ring_check.py``'s own keyword-only ``_violation()`` helper for the
    equivalent ``klt drc``-shaped violation dict.

    ``islands`` (issue #2194) is populated only by the multi-island
    ``erc.unconnected_net`` call site -- the one rule whose subject is
    several distinct places in the layout rather than one -- and is
    ``None`` everywhere else, so the key set stays uniform across rules."""
    return {
        "rule": rule,
        "description": description,
        "net": net,
        "other_net": other_net,
        "gate_id": gate_id,
        "layer": layer,
        "bbox": bbox,
        "islands": islands,
    }


def _antenna_remedy(
    levels: list[dict[str, Any]], index: int, net: str | None
) -> dict[str, Any]:
    """``levels[].remedy`` for a single ``verdict: "violate"`` level (issue
    #908, epic #713 Phase 3 -- "fix guidance"): recommend the standard
    antenna-violation fix -- diode insertion or layer jumping -- naming the
    specific net and layer, so the violation is directly actionable rather
    than just flagged.

    Layer jumping is recommended only when both hold:

    - **the violation is layer-local**: the immediately preceding
      ``stackup`` level (``levels[index - 1]``, when one exists) does *not*
      itself violate. When a lower level already violates, that excess is
      already baked into every level above it (``cumulative_area_um2``
      only ever grows going up the stackup) -- redistributing this net's
      routing to an adjacent layer cannot undo a violation that originated
      lower down, so jumping is not a real fix there.
    - **an adjacent stackup level has margin**: either neighbour
      (``levels[index - 1]`` or ``levels[index + 1]``, whichever exists)
      reports ``verdict == "pass"`` -- i.e. it has headroom under its own
      limit, so shifting more of this net's routing onto that layer is a
      real option. The higher neighbour is preferred when both qualify
      (continuing the route forward, onto the next fabrication step, is
      the more common real remedy); the lower neighbour is used when only
      it has margin (e.g. the violating level is the last in the stackup).

    Every other case falls back to diode insertion -- the general-purpose
    remedy (an antenna diode bleeds off accumulated charge regardless of
    which layer is at fault), used whenever the layer-jumping
    preconditions above are not met.
    """
    level = levels[index]
    layer = level["layer"]
    ratio = level["antenna_ratio"]
    limit = level["antenna_ratio_max"]
    # `net` is `None` for an unlabelled gate (see `gates[].net`) -- name it
    # in prose rather than interpolating the bare Python `None` repr.
    net_desc = f"net {net!r}" if net is not None else "this unlabelled net"

    lower = levels[index - 1] if index > 0 else None
    higher = levels[index + 1] if index + 1 < len(levels) else None

    layer_local = lower is None or lower["verdict"] != "violate"
    higher_margin = higher is not None and higher["verdict"] == "pass"
    lower_margin = lower is not None and lower["verdict"] == "pass"

    if layer_local and (higher_margin or lower_margin):
        target = higher if higher_margin else lower
        return {
            "type": "layer_jumping",
            "net": net,
            "layer": layer,
            "target_layer": target["layer"],
            "justification": (
                f"{net_desc} exceeds the antenna-ratio limit at {layer!r} "
                f"(ratio {ratio:.3g} > {limit:.3g}); {target['layer']!r} has "
                f"margin (ratio {target['antenna_ratio']:.3g} <= "
                f"{target['antenna_ratio_max']:.3g}) -- route more of this "
                f"net's connection through {target['layer']!r} instead of "
                f"continuing to accumulate area on {layer!r}"
            ),
        }

    if layer_local:
        reason = (
            "no adjacent stackup level has margin to absorb the excess "
            "routing (a layer-local violation with no jump target)"
        )
    else:
        reason = (
            "the violation is already present at the preceding stackup "
            f"level ({lower['layer']!r}), so it carries forward regardless "
            f"of how routing on {layer!r} is redistributed"
        )
    return {
        "type": "diode_insertion",
        "net": net,
        "layer": layer,
        "target_layer": None,
        "justification": (
            f"{net_desc} exceeds the antenna-ratio limit at {layer!r} "
            f"(ratio {ratio:.3g} > {limit:.3g}); {reason} -- insert an "
            f"antenna diode on this net at {layer!r} to bleed off "
            "accumulated charge"
        ),
    }


def _floating_gate_findings(
    floating_gates: list[tuple[dict[str, Any], Any, bool]], gate_role: str
) -> list[dict[str, Any]]:
    """``erc.floating_gate`` findings (issue #861): every gate whose
    accumulation stops immediately after the gate role -- zero connected
    area on every ``stackup`` role above it -- is an uncontacted/floating
    gate. Needs no additional spec section.

    ``floating_gates`` is one ``(gate_entry, gate_region, is_floating)``
    triple per discovered gate, in ``gates[]`` order. The predicate is
    passed in rather than re-derived from ``gate_entry["levels"]`` because
    a ``findings_only`` run (issue #2219) never accumulates those per-level
    areas -- it evaluates the same predicate directly off the connectivity
    graph (:func:`_gate_is_floating`), so this rule's findings are identical
    either way."""
    findings: list[dict[str, Any]] = []
    for gate_entry, gate_region, is_floating in floating_gates:
        if not is_floating:
            continue
        findings.append(
            _finding(
                "erc.floating_gate",
                (
                    "gate net has no connected geometry above the gate "
                    "layer (floating/uncontacted gate)"
                ),
                net=gate_entry["net"],
                gate_id=gate_entry["gate_id"],
                layer=gate_role,
                bbox=_bbox_dict(gate_region.bbox()),
            )
        )
    return findings


def _levels_report_floating(levels: list[dict[str, Any]]) -> bool:
    """:func:`_floating_gate_findings`'s predicate read off a fully
    accumulated ``levels[]`` list: zero ``step_area_um2`` on every role
    above the gate."""
    if len(levels) <= 1:
        return False
    return all(level["step_area_um2"] == 0.0 for level in levels[1:])


def _gate_is_floating(
    l2n: Any,
    net: Any,
    stackup: list[dict[str, Any]],
    layer_index: dict[str, int],
    dbu2_um2: float,
) -> bool:
    """:func:`_levels_report_floating`'s predicate computed *without* the
    per-level accumulation (issue #2219), for a ``findings_only`` run.

    Identical answer -- each probe measures exactly the quantity
    :func:`_accumulated_levels` would have stored as that level's
    ``step_area_um2`` (``Region.area()`` applies merged semantics by
    default, so an explicit ``.merged()`` would not change the number),
    and the same ``round(..., 9)`` is applied, so the two paths cannot
    disagree even on an area small enough to round to ``0.0``.

    Cheaper because it **stops at the first role that carries any area**.
    An ordinary strapped gate exits after one probe instead of walking the
    whole stackup, and nothing beyond the predicate's own answer is
    computed: no running cumulative, no ratio, no limit lookup, no remedy
    pass. Only a genuinely floating gate -- the rare case, and the one the
    rule exists to report -- still costs a probe per role."""
    for entry in stackup[1:]:
        region = l2n.polygons_of_net(net, layer_index[entry["name"]])
        if round(region.area() * dbu2_um2, 9) != 0.0:
            return False
    return len(stackup) > 1


def _accumulated_levels(
    l2n: Any,
    net: Any,
    stackup: list[dict[str, Any]],
    layer_index: dict[str, int],
    gate_poly_region: Any,
    gate_area_um2: float,
    dbu2_um2: float,
    antenna_limits: dict[str, tuple[float, str]] | None,
    pdk: str | None,
) -> list[dict[str, Any]]:
    """One gate net's full ``levels[]``: the layer-by-layer accumulation of
    connected conductor area (Phase 1a), each non-gate level's antenna-ratio
    verdict against the resolved PDK limit (Phase 1b), and each violating
    level's remedy (Phase 3).

    This is ``klt erc``'s expensive inner loop -- one
    ``polygons_of_net(...).merged().area()`` per ``stackup`` role per gate
    net -- and the work a ``findings_only`` run skips entirely (issue
    #2219, see :func:`_skipped_accumulation_levels`). ``gate_poly_region``
    is the caller's already-merged gate-role region, reused as
    ``stackup[0]``'s own step region rather than re-extracted: the two are
    the same region by construction (same net, same layer index), so this
    is a free level off the walk.
    """
    levels: list[dict[str, Any]] = []
    cumulative_um2 = 0.0
    for i, entry in enumerate(stackup):
        if i == 0:
            step_region = gate_poly_region
        else:
            step_region = l2n.polygons_of_net(net, layer_index[entry["name"]]).merged()
        step_um2 = step_region.area() * dbu2_um2
        cumulative_um2 += step_um2

        # `cumulative_area_um2 / gate_area_um2` per docs/cli/erc.md's
        # own Phase-1a "Phase scope" note on what 1b delivers. The gate
        # role itself (i == 0) is never PDK-checked: its ratio is
        # trivially 1.0 (cumulative == gate area at that level), and
        # the source table's own poly rule measures a different
        # quantity entirely (poly perimeter, not cumulative connected
        # area) -- not something this area-only model computes.
        antenna_ratio = round(cumulative_um2 / gate_area_um2, 6)
        limit_entry = (
            None
            if i == 0 or antenna_limits is None
            else antenna_limits.get(entry["name"])
        )
        if limit_entry is not None:
            limit_value, rule_id = limit_entry
            antenna_ratio_max: float | None = limit_value
            antenna_ratio_source: str | None = (
                f"{_ANTENNA_SOURCE_URL_BY_PDK[pdk]} rule {rule_id!r}, "
                "'Max EA/A w/o diode' column"
            )
            verdict = "violate" if antenna_ratio > limit_value else "pass"
        else:
            antenna_ratio_max = None
            antenna_ratio_source = None
            verdict = "unchecked"

        levels.append(
            {
                "layer": entry["name"],
                "step_area_um2": round(step_um2, 9),
                "cumulative_area_um2": round(cumulative_um2, 9),
                "antenna_ratio": antenna_ratio,
                "antenna_ratio_max": antenna_ratio_max,
                "antenna_ratio_source": antenna_ratio_source,
                "verdict": verdict,
            }
        )

    # Fix guidance (issue #908, epic #713 Phase 3) -- a second pass over
    # the now-complete `levels` list, since a remedy for level `i` may
    # look at `levels[i + 1]` (see `_antenna_remedy`). `None` for every
    # non-violating level (including every level when `pdk` was omitted,
    # since `verdict` is then always "unchecked").
    for i, level in enumerate(levels):
        level["remedy"] = (
            _antenna_remedy(levels, i, net.name or None)
            if level["verdict"] == "violate"
            else None
        )
    return levels


def _skipped_accumulation_levels(
    stackup: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """One gate net's ``levels[]`` for a ``findings_only`` run (issue
    #2219): every role still enumerated, in fabrication order, but with
    every accumulated quantity ``null`` -- the accumulation those numbers
    come from was never performed.

    ``null`` rather than a plausible-looking ``0.0`` on purpose: a zero
    area is a real, checkable measurement ("nothing connects here") and is
    exactly the measurement ``erc.floating_gate`` keys off, so reporting it
    for work that never ran would make an un-accumulated report
    indistinguishable from a layout of entirely floating gates. Every
    level's ``verdict`` is ``"unchecked"`` and every one lands in
    ``coverage.skipped`` with reason :data:`REASON_FINDINGS_ONLY`, so the
    envelope says which antenna work was declined and why.
    """
    return [
        {
            "layer": entry["name"],
            "step_area_um2": None,
            "cumulative_area_um2": None,
            "antenna_ratio": None,
            "antenna_ratio_max": None,
            "antenna_ratio_source": None,
            "verdict": "unchecked",
            "remedy": None,
        }
        for entry in stackup
    ]


def _antenna_verdict(levels: list[dict[str, Any]]) -> str:
    """One gate's aggregate ``antenna_verdict`` (issue #1997).

    Only ``levels[1:]`` (the non-gate roles) count towards "graded"
    coverage: ``levels[0]`` (the gate role itself) is *always*
    ``"unchecked"`` by construction (see :func:`_accumulated_levels`), so
    its presence must never, on its own, downgrade an otherwise
    fully-graded gate to ``"pass_partial"``. Among the graded levels:
    ``"violate"`` wins outright regardless of coverage; absent a violation,
    ``"pass_partial"`` reports that at least one graded level passed but at
    least one other graded level's own role wasn't in the selected PDK's
    limit table (e.g. sky130's table has no met3-5 entries -- see "Sky130
    antenna-ratio limits" in docs/cli/erc.md) or ``--pdk`` was omitted
    entirely for some otherwise-checkable subset; plain ``"pass"`` only
    when every graded level was actually compared against a limit and none
    violated; ``"unchecked"`` when no graded level was ever compared at all
    (e.g. ``--pdk`` omitted, a ``findings_only`` run, or a single-role
    stackup).
    """
    graded_verdicts = {level["verdict"] for level in levels[1:]}
    if "violate" in graded_verdicts:
        return "violate"
    if "pass" in graded_verdicts:
        return "pass_partial" if "unchecked" in graded_verdicts else "pass"
    return "unchecked"


def _match_net_clusters(circuit: Any, name: str) -> list[Any]:
    """Every net in ``circuit`` whose label set includes ``name``, one per
    disconnected electrical island, sorted by ``cluster_id`` for
    deterministic output.

    When two *different* labels land on the same electrically-connected
    net (a short), ``klayout.db.Net.expanded_name()`` joins every label
    into one comma-separated string (e.g. ``"SHORTED_VDD,VSS"``) rather
    than picking just one -- verified empirically for this issue. Matching
    against the *split* label set (rather than plain equality, the way
    ``power.py``'s ``run_power`` matches its un-shorted ``power_nets``) is
    what lets a single declared name still resolve to a shorted net at
    all -- an exact-equality match would silently see zero matches for
    every name folded into a short, masking the short as
    ``erc.unconnected_net`` instead of correctly finding it."""
    return sorted(
        (
            net
            for net in circuit.each_net()
            if net.cluster_id != 0 and name in net.expanded_name().split(",")
        ),
        key=lambda net: net.cluster_id,
    )


def _island_entry(l2n: Any, layer_index: dict[str, int], net: Any) -> dict[str, Any]:
    """One ``erc.unconnected_net`` ``islands[]`` entry (issue #2194): where
    a single disconnected electrical island of a declared net actually is.

    ``bbox`` is the island's whole extent (raw database units, the
    ``_bbox_dict`` convention) unioned across every ``stackup`` role it has
    geometry on; ``layer`` names the role carrying the most of that island's
    area (ties broken by stackup order, so the answer is deterministic), as
    the single most useful layer to open a viewer on; ``shape_count`` is the
    number of merged polygons across those roles -- enough to tell a
    one-shape orphan stub apart from a whole sub-block that failed to
    strap up.

    Only ``stackup`` roles are measured: ``vias`` conductors are registered
    without their index being kept (see :func:`_extract_connectivity`), and
    a via sits inside the two stackup shapes it joins anyway, so it can
    neither extend the bbox nor be an island's only geometry in practice.
    A labelled net always has stackup geometry by construction -- labels are
    connected to a ``stackup`` conductor region, never to anything else --
    so ``bbox``/``layer`` are never ``None`` for a net this function is
    reached for; the guard exists so a degenerate graph degrades rather
    than raising."""
    bbox: Any = None
    layer: str | None = None
    best_area = 0
    shape_count = 0
    for role, index in layer_index.items():
        region = l2n.polygons_of_net(net, index).merged()
        if region.is_empty():
            continue
        shape_count += region.count()
        role_bbox = region.bbox()
        bbox = role_bbox if bbox is None else bbox + role_bbox
        area = region.area()
        if area > best_area:
            best_area = area
            layer = role
    return {
        "bbox": _bbox_dict(bbox) if bbox is not None else None,
        "layer": layer,
        "shape_count": shape_count,
    }


def _union_bbox(boxes: list[dict[str, int] | None]) -> dict[str, int] | None:
    """The single bbox spanning every non-``None`` entry of ``boxes``, or
    ``None`` when there is none -- used for the multi-island
    ``erc.unconnected_net`` finding's own top-level ``bbox`` (issue #2194),
    which is the extent the split net covers as a whole."""
    present = [box for box in boxes if box is not None]
    if not present:
        return None
    return {
        "left": min(box["left"] for box in present),
        "bottom": min(box["bottom"] for box in present),
        "right": max(box["right"] for box in present),
        "top": max(box["top"] for box in present),
    }


def _net_connectivity_findings(
    l2n: Any,
    circuit: Any,
    layer_index: dict[str, int],
    nets_decl: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """``erc.unconnected_net`` / ``erc.multiply_driven_net`` /
    ``erc.supply_short`` findings (issue #861), driven by the optional
    ``nets`` spec section.

    A declared net matching zero, or a number other than the count its
    entry declares (``nets[].islands``, issue #2400 -- default 1), of
    disconnected electrical islands is ``erc.unconnected_net`` (nothing
    carries that name at all, or the intended net is split into a
    different number of pieces than the spec says to expect -- including
    the pre-#2400 "more than one" case, which is simply the default
    declaration of 1).

    A multi-island finding carries **where** each island is (issue #2194):
    an ``islands[]`` entry per island in the same ``cluster_id`` order
    :func:`_match_net_clusters` returns (see :func:`_island_entry` for the
    per-island shape), plus a top-level ``bbox`` spanning all of them. The
    count alone is only the alarm -- without per-island locations a caller
    has to rebuild this module's own connectivity graph by hand just to find
    out which piece of a multi-hundred-micron block to look at, and two
    reports of the same net cannot be diffed to see *which* island a fix
    resolved. The zero-match case has no geometry to point at and is
    unchanged (``islands`` stays ``None``).

    Two *different* declared net names whose matched nets share a
    ``cluster_id`` are electrically the very same net regardless of their
    separate labels -- a short. Reported per unordered pair (deterministic,
    sorted): ``erc.supply_short`` when both are declared
    ``"kind": "supply"``, else the more general ``erc.multiply_driven_net``.
    """
    if not nets_decl:
        return []

    findings: list[dict[str, Any]] = []
    matches: dict[str, list[Any]] = {}
    for decl in nets_decl:
        matched = _match_net_clusters(circuit, decl["name"])
        matches[decl["name"]] = matched
        # The expected island count is the declared one (issue #2400;
        # ``nets[].islands``, default 1 -- the pre-#2400 "one label string
        # == one electrical net" model). Grading the count against the
        # declaration instead of a hardcoded 1 is what makes this finding
        # falsifiable for a legitimately multi-domain stream: declared N
        # with N actual islands is clean, while declared N with any other
        # actual count -- including a later fragmentation of one domain --
        # still fails.
        expected = decl["islands"]
        if len(matched) == 0:
            findings.append(
                _finding(
                    "erc.unconnected_net",
                    (
                        f"declared net {decl['name']!r} matches no labelled "
                        "geometry in this layout"
                    ),
                    net=decl["name"],
                )
            )
        elif len(matched) != expected:
            islands = [_island_entry(l2n, layer_index, net) for net in matched]
            plural = "" if len(matched) == 1 else "s"
            expected_word = "one" if expected == 1 else str(expected)
            findings.append(
                _finding(
                    "erc.unconnected_net",
                    (
                        f"declared net {decl['name']!r} resolves to "
                        f"{len(matched)} disconnected electrical island{plural} "
                        f"(expected exactly {expected_word})"
                    ),
                    net=decl["name"],
                    bbox=_union_bbox([island["bbox"] for island in islands]),
                    islands=islands,
                )
            )

    cluster_to_names: dict[int, list[str]] = {}
    for decl in nets_decl:
        for net in matches[decl["name"]]:
            cluster_to_names.setdefault(net.cluster_id, []).append(decl["name"])

    kind_by_name = {decl["name"]: decl["kind"] for decl in nets_decl}
    for names in cluster_to_names.values():
        distinct = sorted(set(names))
        if len(distinct) < 2:
            continue
        for i in range(len(distinct)):
            for j in range(i + 1, len(distinct)):
                a, b = distinct[i], distinct[j]
                both_supply = (
                    kind_by_name[a] == "supply" and kind_by_name[b] == "supply"
                )
                rule = "erc.supply_short" if both_supply else "erc.multiply_driven_net"
                findings.append(
                    _finding(
                        rule,
                        (
                            f"declared nets {a!r} and {b!r} are electrically "
                            "the same net (shorted together)"
                        ),
                        net=a,
                        other_net=b,
                    )
                )
    return findings


def _tie_findings(
    l2n: Any, circuit: Any, tie_layers: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """``erc.missing_tie`` findings (issue #861), driven by the optional
    ``ties`` spec section.

    For every physically distinct well/tub shape (each merged polygon of a
    tie's ``well_layer`` -- narrowed to the shapes this tie's class
    selection keeps, issue #2339 -- or of its caller-asserted
    ``well_boxes``, issue #2255; this function cannot and need not tell the
    forms apart, since the region arrives already resolved from
    :func:`_extract_connectivity`), a tap (the
    derived ``tap_layer`` ∩ ``tap_requires`` region, clipped to the well
    itself) must be drawn
    inside it *and* at least one such tap must be electrically connected --
    via the tie connectivity graph, where the tap is wired to
    ``connect_to``'s ``stackup`` region during registration, see
    :func:`_extract_connectivity` -- to the declared ``net``. Both failure
    modes are reported under the same rule id: no tap drawn in the well at
    all, or taps present but none of them wired to the declared net.

    Which taps reach the declared net is answered once per tie, by asking
    the graph for the declared net's *own* polygons on the tap layer
    (``LayoutToNetlist.polygons_of_net``) and then intersecting that set
    against each well shape (issue #2169). This replaces the previous
    single ``probe_net`` of whichever tap happened to come first: with the
    well no longer acting as a blanket conductor merging every tap in it
    into one net (see :func:`_extract_connectivity`), taps in the same well
    are genuinely independent, so a well with one untied and one correctly
    tied tap must still pass -- and it stays a single region operation per
    well rather than a probe per tap.
    """
    if not tie_layers:
        return []

    # Imported lazily, matching this module's/the codebase's convention (see
    # `load_layout`'s docstring) -- needed here for `kdb.Region(polygon)`.
    import klayout.db as kdb

    findings: list[dict[str, Any]] = []
    for tie in tie_layers:
        tied_taps = kdb.Region()
        for net in _match_net_clusters(circuit, tie["net"]):
            tied_taps += l2n.polygons_of_net(net, tie["tap_index"])
        tied_taps = tied_taps.merged()

        for well_poly in tie["well_region"].merged().each():
            poly_region = kdb.Region(well_poly)
            tap_here = tie["tap_region"].interacting(poly_region)
            if tap_here.is_empty():
                findings.append(
                    _finding(
                        "erc.missing_tie",
                        (
                            f"well/tub region has no {tie['name']!r} tap "
                            "contact drawn inside it"
                        ),
                        net=tie["net"],
                        layer=tie["name"],
                        bbox=_bbox_dict(well_poly.bbox()),
                    )
                )
                continue

            if tied_taps.interacting(poly_region).is_empty():
                findings.append(
                    _finding(
                        "erc.missing_tie",
                        (
                            "well/tub tap is not connected to declared net "
                            f"{tie['net']!r}"
                        ),
                        net=tie["net"],
                        layer=tie["name"],
                        bbox=_bbox_dict(well_poly.bbox()),
                    )
                )
    return findings


def _degenerate_tie_reasons(tie_layers: list[dict[str, Any]]) -> dict[str, str]:
    """The declared ties whose ``erc.missing_tie`` verdict is unfalsifiable
    (issues #2199, #2255 and #2339), each mapped to the stable
    ``erc_coverage.skipped[].reason`` naming *why* -- reported as skipped
    work by :func:`_connectivity_coverage` rather than as a check that
    passed.

    There is no device recognition here: a tap and an ordinary source/drain
    contact drawn on the same diffusion/contact layer are the *same
    geometry* to this module. So a tie whose derived tap region is just
    "whatever that layer happens to draw inside the well" reports the well
    as tied as soon as **any** contact in it reaches the declared net --
    which, for a PMOS row whose sources sit on VDD, is every well, every
    time, regardless of whether a tap was ever drawn. The verdict is not
    wrong so much as it is not about taps; the reproduction in issue #2199
    is the same spec and the same GDS answering "tied" for ``vdd`` and
    "untied" for ``vss``.

    A tie is **tap-degenerate** (:data:`REASON_DEGENERATE_TAP_DECLARATION`)
    when all three hold:

    - the spec did not affirm ``tap_is_dedicated`` -- a tap-only layer
      (sky130's ``tap``) needs no narrowing and is a real tap by
      construction;
    - the declared narrowing removed **nothing** from the drawn
      ``tap_layer`` inside the well (``tap_narrowed``, measured in
      :func:`_extract_connectivity`). Omitting ``tap_requires`` is the
      usual way to land here, but a ``tap_requires`` that happens to
      intersect the whole drawn layer in *this* stream (an implant drawn
      over every contact, not only the taps) is exactly as unfalsifiable,
      so the test is geometric rather than a check for the key's presence;
    - the resulting tap region is non-empty. An empty one asserts nothing
      about taps either -- but it cannot pass: every well in it is reported
      as having no tap drawn, which is an honest finding, not a silent
      clean.

    A tie is **well-degenerate** (:data:`REASON_DEGENERATE_WELL_ASSERTION`,
    issue #2255) when it *asserted* its substrate region (``well_layer:
    null`` + ``well_boxes``) instead of naming drawn geometry, and that
    region covers the whole top-cell extent to within
    :data:`_WELL_ASSERTION_MIN_UNCOVERED_FRACTION` (measured in
    :func:`_extract_connectivity`, where both regions exist). "The entire
    die is the substrate" collapses :func:`_tie_findings`'s per-well loop to
    one polygon spanning everything, so the check degrades to "does any
    contact anywhere reach the declared net?" -- satisfied by every PMOS
    source sitting on the rail, tap or not. The same unfalsifiable pass as
    the tap case, one level up. A tie with a *drawn* ``well_layer`` is never
    well-degenerate however much of the die that layer covers: a blanket
    drawn well is a fact about the stream, not an unverifiable claim, and
    rejecting it would break every spec written before this test existed.

    A tie is **selection-degenerate**
    (:data:`REASON_DEGENERATE_WELL_SELECTION`, issue #2339) when it
    *declared* a well-side class selection (``well_requires`` /
    ``well_excludes``) that did not actually partition the drawn
    ``well_layer``: it kept every merged shape on it (the selection
    distinguishes nothing, so the entry grades the other bias class against
    its own ``net`` exactly as an unselected tie would -- the false
    ``erc.missing_tie`` the key exists to remove is still reported, just
    unexplained), or it kept none of them (the per-well loop runs zero
    times, so a zero-finding report means "nothing was examined", which is
    the absence-of-evidence pass every test in this function rejects).
    Unlike the tap test, this one is gated on the key having been *declared*:
    a tie with no well-side selector claims every shape on the layer, which
    is the strongest claim available and the meaning of every spec written
    before this key existed. The measurement itself is geometric, in
    :func:`_select_well_class`, so a selector that happens to keep
    everything in *this* stream is as degenerate as one that names the well
    layer itself.

    **The well tests are applied first**, and win when both hold, because the
    tap measurement is taken *inside* the well region: ``tap_narrowed`` is
    ``(drawn_tap ∩ well) - tap_sites``, so an unfalsifiable well makes the
    tap-side answer a statement about the die rather than about a tap.
    Reporting "narrow your tap" for a declaration whose actual defect is
    "you claimed the whole die" would send a reader to fix the wrong half.

    Deliberately *not* a change to ``erc_findings``: the same findings are
    emitted for the same geometry as before. What changes is that the
    envelope now says the check could not be performed, instead of letting
    ``erc_status: "clean"`` stand for it.
    """
    reasons: dict[str, str] = {}
    for tie in tie_layers:
        if tie["well_degenerate"]:
            reasons[tie["name"]] = REASON_DEGENERATE_WELL_ASSERTION
        elif tie["well_selection_degenerate"]:
            reasons[tie["name"]] = REASON_DEGENERATE_WELL_SELECTION
        elif (
            not tie["tap_is_dedicated"]
            and not tie["tap_narrowed"]
            and not tie["tap_region"].is_empty()
        ):
            reasons[tie["name"]] = REASON_DEGENERATE_TAP_DECLARATION
    return reasons


def _tie_well_region(
    kdb: Any,
    layout: Any,
    top_cell: Any,
    tie: dict[str, Any],
) -> tuple[Any, bool]:
    """This tie's well/substrate region and whether a caller-asserted one is
    degenerate -- ``(well_region, well_degenerate)``.

    Two sources, decided by :func:`_parse_well_declaration` at spec-validation
    time, not here:

    - a drawn ``well_layer`` (every tie before issue #2255) -- the merged
      layer region, exactly as before, never degenerate (see
      :func:`_degenerate_tie_reasons`);
    - ``well_boxes`` (issue #2255) -- the union of the caller's asserted
      micrometre boxes, for a native-substrate block that draws no well/tub
      layer at all. Degenerate when it covers the top cell's own extent to
      within :data:`_WELL_ASSERTION_MIN_UNCOVERED_FRACTION`, i.e. when the
      claim is indistinguishable from "the whole die".

    The assertion is measured against ``top_cell.bbox()`` rather than against
    the die/frame or a caller-supplied extent because that bounding box is
    the one bound this module can derive from the stream itself -- the same
    "verify against geometry, not against the caller's word" discipline
    ``tap_narrowed`` applies on the tap side.

    Split out of :func:`_extract_connectivity` to keep that function under
    the repo's C901 complexity ratchet.
    """
    if not tie["well_boxes"]:
        return _region(layout, top_cell, tie["well_layer"]).merged(), False

    from ._layout import clip_box as _clip_box

    asserted = kdb.Region()
    for box_um in tie["well_boxes"]:
        asserted += kdb.Region(_clip_box(kdb, box_um, layout.dbu))
    asserted = asserted.merged()

    extent = kdb.Region(top_cell.bbox())
    extent_area = extent.area()
    if extent_area <= 0:
        # A top cell with no geometry at all: there is no extent to be
        # distinguishable from, so no assertion about it can be falsified.
        return asserted, True
    uncovered = (extent - asserted).area()
    degenerate = uncovered <= _WELL_ASSERTION_MIN_UNCOVERED_FRACTION * extent_area
    return asserted, degenerate


def _select_well_class(
    layout: Any,
    top_cell: Any,
    tie: dict[str, Any],
    well_region: Any,
) -> tuple[Any, bool]:
    """This tie's well region narrowed to the *class* of well shapes it
    declares -- ``(selected_region, selection_degenerate)``, issue #2339.

    A shape of the drawn ``well_layer`` is kept when it interacts with
    geometry on every ``well_requires`` layer and with none of the
    ``well_excludes`` layers. With neither key declared (every spec before
    this issue) the region is returned untouched and never degenerate: a tie
    with no well-side selector claims every shape on the layer, which is the
    strongest claim available, not the weakest.

    **Selection, not intersection** -- and that is the whole design, not an
    implementation detail. ``tap_requires`` narrows the tap by ``&``-ing
    layers together, because a tap *is* the boolean its PDK draws it as.
    A well is not: the unit :func:`_tie_findings` checks is one merged shape
    of the drawn layer, and the marker that identifies a well's class (a PDK
    device marker over the bipolar it contains, say) generally covers only
    part of that shape. Intersecting would replace the tub with the marker's
    own footprint, shrinking the region the tap must fall inside
    (``tap_sites = tap_region & well_region``) and reporting the tub's real,
    correctly-wired tap ring as "no tap contact drawn inside it" -- the same
    class of false ``erc.missing_tie`` this key exists to remove. Keeping
    whole shapes also keeps each finding's ``bbox`` the well the layout
    actually draws.

    ``interacting`` (touch or overlap) rather than ``overlapping`` (area
    only) matches how :func:`_tie_findings` already tests a tap against its
    well, so "inside it" means one thing throughout this module.

    The degeneracy verdict is measured here, where both regions exist (as
    ``tap_narrowed`` and ``well_degenerate`` are): a *declared* selection
    that keeps every drawn shape, or keeps none of them, did not partition
    anything -- see :data:`REASON_DEGENERATE_WELL_SELECTION` and
    :func:`_degenerate_tie_reasons`.
    """
    if not tie["well_requires"] and not tie["well_excludes"]:
        return well_region, False

    selected = well_region
    for required in tie["well_requires"]:
        selected = selected.interacting(_region(layout, top_cell, required))
    for excluded in tie["well_excludes"]:
        selected = selected.not_interacting(_region(layout, top_cell, excluded))
    selected = selected.merged()
    degenerate = selected.is_empty() or (well_region - selected).is_empty()
    return selected, degenerate


def _warn_label_layer_empty(verb: str, name: str, label_layer: str) -> None:
    """The stderr warning printed once per ``stackup`` entry whose declared
    ``label_layer`` carries **zero** text objects in the analysed top cell
    (issue #2401).

    An empty label layer is silent everywhere else in the report: every
    ``gates[].net`` on that role reads ``null`` (indistinguishable from a
    spec with no ``label_layer`` at all) and every ``nets[]`` entry that
    expects to match through it resolves zero islands and is reported as
    ``erc.unconnected_net`` -- which reads as "this net is not in the
    layout", i.e. a blocking supply-connectivity defect. In practice that
    combination is almost always a mis-transcribed layer/datatype (a
    ``.pin`` polygon layer named where the PDK's ``.label`` text layer was
    meant) rather than a real connectivity failure. Printed to stderr,
    following :func:`klayout_tools._devices._warn_device_body_missed_role`
    's precedent (issue #2226), so a caller piping ``--format json`` to a
    file still sees it (JSON goes to stdout only -- ``docs/json-contract.md``).
    ``verb`` is the calling command (``"klt erc"``), so the line names the
    tool the caller actually ran."""
    print(
        f"{verb}: warning: stackup entry {name!r} declares label_layer "
        f"{label_layer}, but that layer carries no text anywhere in the "
        "analysed top cell -- nets matched through it resolve zero islands "
        "(erc.unconnected_net) and its gates[].net reads null. Check the "
        "label layer/datatype.",
        file=sys.stderr,
    )


def _register_label_layer(
    l2n: Any,
    layout: Any,
    top_cell: Any,
    entry: dict[str, Any],
    conductor_region: Any,
    verb: str | None,
) -> None:
    """Register a ``stackup`` entry's declared ``label_layer`` text onto its
    conductor region in the ``LayoutToNetlist`` graph -- and warn when that
    layer carries zero text objects in the analysed top cell
    (:func:`_warn_label_layer_empty`, issue #2401).

    A ``label_layer`` of ``None`` (the spec default) registers nothing and
    warns nothing, matching the pre-#2401 behaviour byte for byte. ``verb``
    of ``None`` registers the text but skips the warning: the tie-graph
    re-extraction (:func:`_extract_connectivity`'s second caller) walks the
    same ``stackup`` again, and the warning is printed once per affected
    entry, not once per extraction."""
    if entry["label_layer"] is None:
        return
    label_texts = _texts(layout, top_cell, entry["label_layer"])
    if verb is not None and label_texts.is_empty():
        layer, datatype = entry["label_layer"]
        _warn_label_layer_empty(verb, entry["name"], f"{layer}/{datatype}")
    l2n.register(label_texts, f"{entry['name']}_label")
    l2n.connect(conductor_region, label_texts)


def _extract_connectivity(
    layout: Any,
    top_cell: Any,
    stackup: list[dict[str, Any]],
    vias: list[dict[str, Any]],
    ties: list[dict[str, Any]],
    device_cuts: dict[str, Any],
    verb: str | None,
) -> tuple[Any, Any, dict[str, int], list[dict[str, Any]]]:
    """Build, extract, and return one ``LayoutToNetlist`` connectivity graph
    over the declared ``stackup``/``vias`` -- plus, when ``ties`` is
    non-empty, each tie's derived *tap* conductor.

    A ``stackup`` entry whose declared ``label_layer`` carries zero text
    objects in the analysed top cell additionally gets a one-line stderr
    warning (:func:`_warn_label_layer_empty`, issue #2401) -- the
    mis-transcribed-layer case the report alone renders as
    ``erc.unconnected_net`` on every net matched through it.

    Returns ``(l2n, circuit, layer_index, tie_layers)``.

    **A declared device body is not a wire (issue #2183).** ``device_cuts``
    (from :func:`_device_body_cuts`, ``{}`` when the spec declares no
    ``devices[]``) maps a ``stackup``/``vias`` role to the merged region of
    the device bodies drawn on it; each role's conductor region is
    registered with that region subtracted, so a rail-to-rail poly-resistor
    string breaks the net at the resistor body instead of conducting
    through it and inventing an ``erc.supply_short`` between the two
    supplies it deliberately spans. Nothing else about the graph changes:
    with ``devices[]`` omitted, ``device_cuts`` is empty and every region
    below is the raw drawn layer, exactly as before.

    **A declared well is never a conductor here (issue #2169).** The
    previous model registered the whole ``well_layer`` region, self-
    connected it (``l2n.connect(well_region)``) and connected it to the tap
    layer, which made a blanket well -- one plan-view polygon spanning
    whole standard-cell rows -- conduct to *every* shape that merely
    overlapped it, not only to the shapes actually tied to it. On a routed
    design that collapsed the entire layout into one or two electrical
    islands: a false ``erc.supply_short`` between VDD and VSS, and a
    ``gates[]`` list collapsed to a single entry (which silently invalidated
    every antenna ratio in the same report).

    Instead, a tie contributes exactly one conductor: its **tap sites** --
    ``tap_layer`` intersected with every ``tap_requires`` layer (the
    ``Comp ∩ Nplus``-style boolean a real PDK tap is drawn as), further
    intersected with the union of ``tap_boxes`` when the spec asserts them
    (issue #2234), and then clipped to the well itself, since a tap is by
    definition inside the well it taps. Those sites are wired up to
    ``connect_to``'s ``stackup`` region exactly as before, so a tap still
    reaches (or fails to reach) the declared supply net through real
    routing. The well contributes only *where the taps are*, never across
    its own extent, and taps in the same well are not shorted to each other
    through it.

    **A well need not be drawn at all (issue #2255).** A tie declaring
    ``well_layer: null`` + ``well_boxes`` -- a native-substrate block, whose
    substrate is diffusion-derived rather than layer-marked -- resolves its
    well region from those asserted boxes instead of from a layer
    (:func:`_tie_well_region`), and everything downstream is identical: the
    well is still not a conductor, the taps are still clipped to it, and the
    same ``tap_narrowed`` measurement is still taken inside it. What the
    assertion additionally carries is its own degeneracy verdict
    (``well_degenerate``), measured here against the top cell's own extent.

    **A drawn well may hold more than one bias class (issue #2339).** When
    the tie declares ``well_requires``/``well_excludes``, only the merged
    well shapes of that class survive into everything above
    (:func:`_select_well_class`) -- so the taps clipped to the well, the
    ``tap_narrowed`` measurement taken inside it, and the per-well loop in
    :func:`_tie_findings` all see one bias class rather than every shape the
    tub layer draws. A second, complementary entry declares the other class
    against its own ``net``, and the two are graded independently, keyed on
    the tie ``name`` rather than on the shared ``well_layer``.
    """
    # Imported lazily, matching `load_layout`'s lazy `klayout.db` import.
    import klayout.db as kdb

    from ._layout import clip_box as _clip_box

    l2n = kdb.LayoutToNetlist(top_cell.name, layout.dbu)

    layer_index: dict[str, int] = {}
    regions: dict[str, Any] = {}
    for entry in stackup:
        conductor_region = _cut_device_bodies(
            _region(layout, top_cell, entry["layer"]), device_cuts.get(entry["name"])
        )
        regions[entry["name"]] = conductor_region
        layer_index[entry["name"]] = l2n.register(conductor_region, entry["name"])
        l2n.connect(conductor_region)
        _register_label_layer(l2n, layout, top_cell, entry, conductor_region, verb)

    for via in vias:
        via_region = _cut_device_bodies(
            _region(layout, top_cell, via["layer"]), device_cuts.get(via["name"])
        )
        l2n.register(via_region, via["name"])
        l2n.connect(via_region)
        role_a, role_b = via["between"]
        l2n.connect(regions[role_a], via_region)
        l2n.connect(via_region, regions[role_b])

    tie_layers: list[dict[str, Any]] = []
    for tie in ties:
        # Drawn `well_layer`, or -- for a native-substrate block that draws
        # none -- the caller's asserted `well_boxes` region plus whether
        # that assertion is degenerate (issue #2255). Everything below is
        # identical either way: the well is the well, however it was named.
        well_region, well_degenerate = _tie_well_region(kdb, layout, top_cell, tie)
        # `well_requires`/`well_excludes` (issue #2339): keep only the merged
        # well shapes of the class this tie declares, so one tub layer
        # carrying two differently-biased classes can be declared as two
        # complementary entries. A no-op unless the spec asked for it.
        well_region, well_selection_degenerate = _select_well_class(
            layout, top_cell, tie, well_region
        )
        drawn_tap = _region(layout, top_cell, tie["tap_layer"])
        tap_region = drawn_tap
        for required in tie["tap_requires"]:
            tap_region = tap_region & _region(layout, top_cell, required)
        # `tap_boxes` (issue #2234): a caller assertion of exactly where the
        # tap sites are, as literal micrometre boxes -- composes with
        # `tap_requires` (both narrow the same region) but needs no drawn
        # PDK marker layer to exist at all.
        if tie["tap_boxes"]:
            asserted_region = kdb.Region()
            for box_um in tie["tap_boxes"]:
                asserted_region += kdb.Region(_clip_box(kdb, box_um, layout.dbu))
            tap_region = tap_region & asserted_region.merged()
        tap_sites = (tap_region & well_region).merged()
        tap_index = l2n.register(tap_sites, f"{tie['name']}__tap")
        l2n.connect(tap_sites)
        l2n.connect(tap_sites, regions[tie["connect_to"]])
        tie_layers.append(
            {
                **tie,
                "well_region": well_region,
                "tap_region": tap_sites,
                "tap_index": tap_index,
                # Issue #2199: measured here, where both regions exist,
                # rather than re-derived later from the spec alone -- see
                # `_degenerate_tie_reasons`. `tap_boxes` narrowing feeds the
                # same measurement as `tap_requires` -- an assertion that
                # removes nothing from the drawn `tap_layer` inside the well
                # is exactly as degenerate as omitting it (issue #2234).
                "tap_narrowed": not (
                    (drawn_tap & well_region).merged() - tap_sites
                ).is_empty(),
                # Whether this tie's tap region was derived (at least in
                # part) from a caller assertion rather than pure PDK-marker
                # narrowing -- read by `_connectivity_coverage` to grade a
                # non-degenerate asserted tie as `checked_by_assertion`
                # (issue #2234).
                "tap_asserted": bool(tie["tap_boxes"]),
                # Issue #2255: whether this tie's *well* region was asserted
                # by the caller (`well_layer: null` + `well_boxes`) rather
                # than read off a drawn layer, and whether that assertion is
                # indistinguishable from the whole top-cell extent. Read by
                # `_degenerate_tie_reasons` and by `_connectivity_coverage`'s
                # `checked_by_well_assertion`.
                "well_asserted": bool(tie["well_boxes"]),
                "well_degenerate": well_degenerate,
                # Issue #2339: whether this tie's declared well-side class
                # selection kept a proper, non-empty subset of the drawn
                # `well_layer` -- `False` for every tie that declared none.
                # Read by `_degenerate_tie_reasons`.
                "well_selection_degenerate": well_selection_degenerate,
            }
        )

    try:
        l2n.extract_netlist()
    except Exception as exc:  # KLayout raises a bare RuntimeError on internal failure
        raise ErcError(f"connectivity extraction failed: {exc}") from exc

    circuit = l2n.netlist().circuit_by_name(top_cell.name)
    if circuit is None:
        raise ErcError(
            f"no circuit named '{top_cell.name}' in the extracted connectivity graph"
        )
    return l2n, circuit, layer_index, tie_layers


def run_erc(
    file: str,
    spec_path: str,
    *,
    top: str | None = None,
    pdk: str | None = None,
    deck: str | None = None,
    findings_only: bool = False,
) -> dict[str, Any]:
    """Run ``klt erc``'s connectivity-model extraction, antenna-ratio
    check, and core ERC finding checks end to end.

    ``file`` is a routed GDSII/OASIS layout; ``spec_path`` is a JSON file
    with:

    - ``stackup`` (required, >= 2 entries): fabrication order from the gate
      layer up. Each entry is ``{"name", "layer": "<layer>/<datatype>",
      "label_layer": "<layer>/<datatype>" (optional)}``; ``stackup[0]``
      additionally sets ``"role": "gate"`` and may optionally set
      ``"active_layer": "<layer>/<datatype>"`` (issue #1979) -- the
      diffusion/active layer used to compute true gate area as ``poly ∩
      diff`` rather than raw poly-net area, so a tie-cell/decap/filler-cell
      poly resistor (no diffusion under it) is correctly excluded from
      ``gates[]`` instead of producing a false antenna-ratio violation.
      Omitted -> today's raw-poly-area behaviour, unchanged.
    - ``vias`` (optional array, default ``[]``): each entry bridges two
      ``stackup`` names -- ``{"name" (optional, defaults to "via<index>"),
      "layer": "<layer>/<datatype>", "between": ["<role>", "<role>"]}``.
    - ``nets`` (optional array, default ``[]``, issue #861): named nets to
      check for connectivity findings -- ``{"name",
      "kind": "signal" | "supply"` (default ``"signal"``)}``. Drives the
      ``erc.unconnected_net`` / ``erc.multiply_driven_net`` /
      ``erc.supply_short`` findings; omitted entirely -> none of those
      three are computed.
    - ``ties`` (optional array, default ``[]``, issue #861): substrate/well
      tie declarations -- ``{"name" (optional, defaults to "tie<index>"),
      "well_layer": "<layer>/<datatype>" | null, "well_boxes": [[left,
      bottom, right, top], ...] (optional, issue #2255), "well_requires":
      ["<layer>/<datatype>", ...] (optional, issue #2339),
      "well_excludes": ["<layer>/<datatype>", ...] (optional, issue #2339),
      "tap_layer":
      "<layer>/<datatype>", "tap_requires": ["<layer>/<datatype>", ...]
      (optional, issue #2169), "tap_is_dedicated": <bool> (optional, issue
      #2199), "tap_boxes": [[left, bottom, right, top], ...] (optional,
      issue #2234), "connect_to": "<stackup role>", "net"}``. Drives the
      ``erc.missing_tie`` finding; omitted entirely -> none are computed.
      ``tap_requires`` intersects further layers into the tap so the spec
      can name the boolean a real PDK tap is drawn as (``Comp ∩ Nplus``);
      ``tap_is_dedicated`` says the ``tap_layer`` is already tap-only and
      needs no narrowing. A tie that says neither, and whose tap region is
      therefore whatever that layer draws inside the well, is graded as
      *skipped* work in ``erc_coverage`` (see
      :func:`_degenerate_tie_reasons`). ``well_layer: null`` +
      ``well_boxes`` is the native-substrate form (issue #2255): a block
      that draws no well/tub layer asserts the substrate region instead of
      naming one, graded under ``erc_coverage.checked_by_well_assertion``
      and skipped as degenerate when the asserted region is
      indistinguishable from the whole top-cell extent.
      ``well_requires``/``well_excludes`` (issue #2339) are the well-side
      class selectors for a *drawn* well layer that carries two
      differently-biased classes: each merged well shape is kept only when
      it interacts with geometry on every ``well_requires`` layer and with
      none of the ``well_excludes`` layers, so each class is declarable as
      its own entry against its own ``net`` instead of one entry reporting
      every well of the other class as untied. A declared selection that
      keeps every drawn shape, or none of them, is skipped as degenerate.
      Ties are extracted
      in their own connectivity graph (:func:`_extract_connectivity`), so
      they affect ``erc.missing_tie`` and nothing else.
    - ``devices`` (optional array, default ``[]``, issue #2183): where a
      drawn *device body* sits on an already-declared conductor role --
      ``{"name" (optional, defaults to "device<index>"), "body_layer":
      "<layer>/<datatype>", "on": "<stackup or vias name>"}``. Each
      entry's region is subtracted from that role's conductor region
      before it is registered, so a rail-to-rail poly-resistor string (a
      power-on-reset divider, a brown-out detector, a bias string) breaks
      the net at the device body instead of reading as a dead short
      between the two supplies it spans. Omitted entirely -> no carve-out,
      today's behaviour exactly. What was applied (and the area each
      declaration actually removed) is echoed in ``provenance.devices``.
      An explicit entry always wins over a ``deck``-detected carve-out for
      the same role (issue #2204, see below).

    ``top`` selects the top cell to analyse when the stream has more than
    one (required in that case, matching ``select_top_cells``'s convention
    -- see ``docs/cli/layers.md``'s ``--top``).

    ``pdk`` (optional, e.g. ``"sky130"``) selects the antenna-ratio limit
    table each non-gate ``stackup`` level's ``antenna_ratio`` is compared
    against -- see :data:`_SKY130_ANTENNA_RATIO_MAX_EGAR`. Omit it to still
    get every level's derived ``antenna_ratio``, just with ``verdict``
    ``"unchecked"`` everywhere (no PDK limit to compare against).

    ``deck`` (optional, e.g. ``"gf180mcu"``, issue #2204) selects a curated
    extraction deck (:func:`~klayout_tools.decks.get_extraction_deck` --
    the same registry ``klt extract --deck``/``klt lvs --deck`` resolve,
    needing no PDK install) whose own ``ResistorDevice``/``CapacitorDevice``
    declarations are matched against the declared ``stackup``/``vias``
    roles and auto-carved out exactly where ``devices[]`` would otherwise
    have to name them by hand -- see :func:`_deck_device_cuts` for the
    matching rule and precedence, and ``docs/cli/erc.md``'s "Deck-driven
    device-marker auto-detection" for the full picture. Deliberately
    independent of ``pdk``: ``pdk`` selects only the antenna-ratio limit
    table, has no ``gf180mcu`` entry, and resolves nothing when a deck name
    would be valid for extraction but not for the antenna table (or vice
    versa). Omitted (the default) -> no auto-detection is attempted, and
    every output this feature could touch (``gates[]``, every antenna
    ratio, every finding, ``provenance.deck``, ``provenance.devices``) is
    byte-identical to a run before this feature existed.

    ``findings_only`` (optional, default ``False``, issue #2219) asks for
    the ``erc_findings`` half of this report only, and **skips the per-gate
    per-level antenna accumulation** that dominates its runtime: one
    ``polygons_of_net(...).merged().area()`` per gate net per ``stackup``
    role, which on a dense layout (tens of thousands of gate nets) is
    almost the entire wall clock. Every ``erc_findings`` rule is still
    evaluated, and every finding is identical to the same run without the
    flag -- the four ``nets[]``/``ties[]``-driven rules never read the
    accumulated areas at all, and ``erc.floating_gate``'s predicate is
    evaluated directly off the connectivity graph instead
    (:func:`_gate_is_floating`). What changes is the *antenna* half:
    ``gates[].levels[]`` still enumerates every role, but with
    ``step_area_um2``/``cumulative_area_um2``/``antenna_ratio`` reported as
    ``None`` (the measurement was not taken -- see
    :func:`_skipped_accumulation_levels`), every ``verdict``
    ``"unchecked"``, every level in ``coverage.skipped`` with reason
    :data:`REASON_FINDINGS_ONLY`, and therefore ``status:
    "not_checked"``/exit 4 -- exactly the antenna answer a ``--pdk``-less
    run already gives. ``erc_status``/``erc_coverage`` are unaffected and
    stay fully graded. Passing both ``findings_only`` and ``pdk`` is a
    contradiction (a limit table with nothing to grade) and raises
    :class:`ErcError` rather than silently returning an ungraded antenna
    half to a caller who asked for one.

    Returns a dict matching the documented ``klt erc`` JSON schema (see
    ``docs/cli/erc.md``), including ``schema_version``, (issue #861)
    ``erc_findings``/``erc_finding_count``, (issue #908) each violating
    ``levels[].remedy``, and (issue #1968) a top-level ``status`` (
    ``"clean"`` only when ``erc_finding_count == 0`` *and* no
    ``gates[].levels[].verdict`` is ``"violate"``, else ``"violations"``)
    plus the shared ``provenance`` block (:func:`._provenance.build_provenance`)
    -- together the two things ``klt signoff`` needs to grade this output.
    That block additionally carries (issue #2036) a verb-local
    ``provenance.spec.content_hash``, pinning ``spec_path``'s contents the
    same ``sha256:``-prefixed way ``provenance.input.content_hash`` pins the
    layout, so a committed report can be re-verified against *both* inputs
    its verdict depends on.

    Also returned (issue #2179): ``erc_status``/``erc_coverage`` -- the
    connectivity half's own roll-up, graded on ``erc_findings`` and the
    ``nets[]``/``ties[]`` work actually declared, so "the connectivity rules
    ran and passed" stays readable on a PDK with no antenna-ratio table at
    all, where ``status`` is necessarily ``"not_checked"``. See
    :func:`_connectivity_coverage`. A ``ties[]`` entry whose tap region is
    indistinguishable from an ordinary source/drain contact makes that
    roll-up ``"clean_partial"`` rather than ``"clean"`` (issue #2199, see
    :func:`_degenerate_tie_reasons`).

    Raises :class:`ErcError` for a malformed spec, an unknown ``pdk`` or
    ``deck``, a ``findings_only`` run that also names a ``pdk``, an
    unresolvable layout/top cell, or a layout in which no net carries any
    geometry on the declared gate role at all.
    """
    if findings_only and pdk is not None:
        raise ErcError(
            "--findings-only skips the per-gate antenna accumulation "
            f"entirely, so --pdk {pdk!r} would have nothing to grade -- "
            "pass --findings-only for the erc_findings-only read, or --pdk "
            "for the antenna verdict, but not both"
        )
    antenna_limits = _resolve_antenna_limits(pdk)
    deck_obj = _resolve_deck(deck)

    spec = _load_spec_json(spec_path, ErcError)
    stackup = _validate_stackup(spec, spec_path)
    stackup_names = [entry["name"] for entry in stackup]
    vias = _validate_vias(spec, spec_path, stackup_names)
    nets_decl = _validate_nets(spec, spec_path)
    ties = _validate_ties(spec, spec_path, stackup_names)
    ties_disclosure = _validate_ties_disclosure(spec, spec_path)
    # The `devices[]` fragment is shared verbatim with `klt power` (issue
    # #2260), so it lives in `_devices.py` and takes the flat list of
    # conductor role names -- `stackup` first, then `vias` -- this module
    # would otherwise assemble internally.
    devices = _validate_devices(
        spec,
        spec_path,
        stackup_names + [via["name"] for via in vias],
        ErcError,
    )

    layout = load_layout(file, ErcError)
    top_cells = select_top_cells(layout, top, ErcError)
    if len(top_cells) != 1:
        raise ErcError(
            f"klt erc needs exactly one top cell to analyse ({len(top_cells)} "
            f"found in '{file}'); pass --top to select one"
        )
    top_cell = top_cells[0]
    dbu = layout.dbu

    # Two graphs, deliberately (issue #2169). The *primary* graph carries
    # only what the caller declared as real routing -- `stackup` + `vias` --
    # and is the sole source of `gates[]`, the antenna-ratio model, and the
    # `nets[]`-driven findings. The `ties[]` declarations are extracted
    # separately below, so a mis-declared well/tap layer can no longer
    # silently collapse `gates[]` (and with it every antenna ratio in the
    # same report) or invent an `erc.supply_short` between two rails that
    # are not actually shorted: with `ties` omitted or declared, these
    # outputs are identical by construction, not merely by convention.
    # `devices[]` (issue #2183): the declared device bodies, resolved once
    # and subtracted from their own role in *both* graphs below -- a drawn
    # resistor body is not a wire in the tie graph either. Empty (and so a
    # no-op) whenever the spec declares no `devices`.
    # `role_layers` (role name -> its declared `(layer, datatype)`) is what
    # lets `_device_body_cuts` report the area each declaration *actually*
    # subtracted -- `marker ∩ that role's own drawn region` -- rather than
    # the marker layer's own area (issue #2226). `--deck` reuses the same
    # map below for its exact-layer-equality matching rule.
    role_layers = _conductor_role_layers(stackup, vias)
    device_cuts, devices_applied = _device_body_cuts(
        layout, top_cell, devices, role_layers, verb="klt erc"
    )

    # `--deck` (issue #2204): a curated deck's own device-marker
    # declarations, auto-carved out wherever a marker's conducting-body
    # layer matches a declared role's own layer exactly -- see
    # `_deck_device_cuts` for the matching rule. An explicit `devices[]`
    # entry for a role always wins (that role is passed in `declared_roles`
    # below, so `_deck_device_cuts` never computes a cut for it, only a
    # `superseded_by`-marked provenance echo). No-op when `--deck` was
    # omitted -- `deck_obj` is `None`, and neither `device_cuts` nor
    # `devices_applied` is touched, matching this feature's own
    # byte-identical-when-unused acceptance criterion.
    if deck_obj is not None:
        declared_roles: dict[str, str] = {}
        for device in devices:
            declared_roles.setdefault(device["on"], device["name"])
        deck_cuts, deck_devices_applied = _deck_device_cuts(
            layout, top_cell, deck_obj, role_layers, declared_roles
        )
        for role, region in deck_cuts.items():
            device_cuts[role] = (
                (device_cuts[role] + region).merged() if role in device_cuts else region
            )
        devices_applied = [
            {**entry, "source": "declared", "superseded_by": None}
            for entry in devices_applied
        ] + deck_devices_applied

    l2n, circuit, layer_index, _ = _extract_connectivity(
        layout, top_cell, stackup, vias, [], device_cuts, verb="klt erc"
    )

    gate_role = stackup[0]["name"]
    gate_layer_index = layer_index[gate_role]
    dbu2_um2 = dbu * dbu

    # `active_layer` (issue #1979, optional): when the spec supplies a
    # diffusion/active layer on `stackup[0]`, gate identification and the
    # antenna-ratio denominator below are computed from `poly ∩ diff` --
    # true gate (oxide) area -- rather than raw poly-net area. This is a
    # plain geometric intersection against the whole-layout active region,
    # *not* a connectivity-graph registration: diffusion never needs to be
    # traced for connectivity here, only intersected against each gate net's
    # own merged poly region. When omitted, `active_region` stays `None` and
    # every net's raw poly area is used unchanged (today's behaviour) -- see
    # docs/cli/erc.md's "Spec file" section for the documented caveat this
    # leaves for tie/decap/filler-cell nets.
    active_layer = stackup[0]["active_layer"]
    active_region = (
        _region(layout, top_cell, active_layer) if active_layer is not None else None
    )

    # Every distinct net (cluster) is already one electrically-connected
    # island by construction (`LayoutToNetlist` clusters connected geometry
    # into one `Net` per cluster id) -- unlike `klt power`'s caller-named
    # `power_nets`, no name match is needed to find "a gate": any net whose
    # geometry touches the declared gate role qualifies. Sorted by
    # `cluster_id` for deterministic, reproducible output.
    candidates = sorted(
        (net for net in circuit.each_net() if net.cluster_id != 0),
        key=lambda net: net.cluster_id,
    )

    gates: list[dict[str, Any]] = []
    # Parallel to `gates` (same order/length) -- each gate's own merged
    # gate-role region, kept only for `_floating_gate_findings`'s `bbox`
    # (not part of the `gates[]` JSON schema itself). Always the *raw* poly
    # region regardless of `active_layer` -- only the area used for
    # membership/the antenna-ratio denominator below changes with the fix.
    gate_regions: list[Any] = []
    # Parallel to `gates` too -- the `erc.floating_gate` predicate for each
    # gate, computed from the accumulated `levels[]` on the normal path and
    # directly off the connectivity graph on the `findings_only` one (issue
    # #2219). Same answer either way; see `_gate_is_floating`.
    gate_floating: list[bool] = []
    for net in candidates:
        gate_poly_region = l2n.polygons_of_net(net, gate_layer_index).merged()

        # `gate_area_um2` (issue #1979): true gate (oxide) area is `poly ∩
        # diff` when `active_layer` was supplied -- a tie-cell/decap/
        # filler-cell poly resistor has `poly ∩ diff == 0` (no gate oxide)
        # and is correctly excluded below by the existing zero-area skip.
        # Falls back to raw poly-net area (today's behaviour) when
        # `active_layer` is omitted.
        if active_region is not None:
            gate_area_region = (gate_poly_region & active_region).merged()
        else:
            gate_area_region = gate_poly_region
        gate_area_um2 = gate_area_region.area() * dbu2_um2
        if gate_area_um2 <= 0:
            continue

        # The expensive part (issue #2219): one
        # `polygons_of_net(...).merged().area()` per stackup role per gate
        # net. A `findings_only` run never needs those numbers -- none of
        # the `nets[]`/`ties[]`-driven findings read them, and the one rule
        # that does (`erc.floating_gate`) has a cheaper equivalent
        # predicate -- so it is skipped outright rather than computed and
        # then reported `"unchecked"`.
        if findings_only:
            levels = _skipped_accumulation_levels(stackup)
            is_floating = _gate_is_floating(l2n, net, stackup, layer_index, dbu2_um2)
        else:
            levels = _accumulated_levels(
                l2n,
                net,
                stackup,
                layer_index,
                gate_poly_region,
                gate_area_um2,
                dbu2_um2,
                antenna_limits,
                pdk,
            )
            is_floating = _levels_report_floating(levels)

        gates.append(
            {
                "gate_id": f"gate{len(gates)}",
                "net": net.name or None,
                "gate_area_um2": round(gate_area_um2, 9),
                "antenna_verdict": _antenna_verdict(levels),
                "levels": levels,
            }
        )
        gate_regions.append(gate_poly_region)
        gate_floating.append(is_floating)

    if not gates:
        raise ErcError(
            f"no net in '{file}' has any geometry on the declared gate role "
            f"{gate_role!r} (stackup[0], layer {stackup[0]['layer'][0]}/"
            f"{stackup[0]['layer'][1]}) -- check the spec's stackup[0].layer "
            "against the layout's own resolved PDK gate-poly layer number"
        )

    # ERC finding checks (issue #861, Phase 1c) -- see this module's
    # docstring, "ERC finding checks", for what each rule detects.
    erc_findings: list[dict[str, Any]] = []
    erc_findings.extend(
        _floating_gate_findings(
            list(zip(gates, gate_regions, gate_floating, strict=True)), gate_role
        )
    )
    erc_findings.extend(
        _net_connectivity_findings(l2n, circuit, layer_index, nets_decl)
    )

    # The tie graph (issue #2169): a second extraction, built only when the
    # spec actually declares `ties[]`, that adds each tie's derived tap
    # conductor on top of the same stackup/vias. Kept separate from the
    # primary graph above so the `erc.missing_tie` answer can depend on tap
    # geometry without any `ties[]` declaration -- correct, over-broad, or
    # outright wrong -- being able to reach `gates[]`, the antenna ratios,
    # or the `nets[]` findings already computed.
    degenerate_ties: dict[str, str] = {}
    asserted_ties: set[str] = set()
    well_asserted_ties: set[str] = set()
    if ties:
        tie_l2n, tie_circuit, _, tie_layers = _extract_connectivity(
            layout, top_cell, stackup, vias, ties, device_cuts, verb=None
        )
        erc_findings.extend(_tie_findings(tie_l2n, tie_circuit, tie_layers))
        degenerate_ties = _degenerate_tie_reasons(tie_layers)
        # Issue #2234: a non-degenerate tie whose tap region was derived (at
        # least in part) from a caller assertion (`tap_boxes`) rather than
        # pure PDK-marker narrowing -- graded `checked_by_assertion` by
        # `_connectivity_coverage` below, distinct from a bare
        # `tap_requires`/`tap_is_dedicated` pass.
        asserted_ties = {
            tie["name"]
            for tie in tie_layers
            if tie["tap_asserted"] and tie["name"] not in degenerate_ties
        }
        # Issue #2255: the same distinction one level up -- a non-degenerate
        # tie whose *well* region was asserted (`well_layer: null` +
        # `well_boxes`) because the block draws no well/tub layer at all.
        # Its own `checked_by_well_assertion` list, never folded into the
        # tap-side one: asserting which drawn geometry is the tap and
        # asserting the region itself are different claims.
        well_asserted_ties = {
            tie["name"]
            for tie in tie_layers
            if tie["well_asserted"] and tie["name"] not in degenerate_ties
        }
    erc_findings.sort(
        key=lambda f: (
            f["rule"],
            f["net"] or "",
            f["other_net"] or "",
            f["gate_id"] or "",
            f["layer"] or "",
        )
    )

    erc_finding_count = len(erc_findings)

    # Top-level `status` (issue #1968) -- so `klt erc`'s output is gradable
    # (`klt signoff`'s `_check_passed`-style pass/fail read) without having
    # to separately inspect two independent signals. Mirrors `klt drc`'s own
    # `"clean"`/`"violations"` split, but -- unlike `erc_finding_count`
    # alone -- also rolls up every gate's own per-level antenna verdict
    # (`gates[].levels[].verdict`, see `klt power`'s own `em_verdict.status`
    # roll-up from per-edge data for the analogous per-entry-rollup
    # precedent): a run with zero `erc_findings` but at least one antenna
    # `"violate"` level must not read as clean.
    any_antenna_violation = any(
        level["verdict"] == "violate" for gate in gates for level in gate["levels"]
    )
    coverage = _antenna_coverage(gates, pdk, findings_only)

    # The common rollup rule (issue #2109, this adapter's own #2115) decides
    # `status` from `coverage` plus the two violation signals above, rather
    # than re-deriving zero/full/partial locally the way this module did
    # before #2115: `any finding/violation` still wins outright (`"failed"`
    # outranks coverage), known zero graded antenna levels is
    # `"not_checked"`/exit 4 exactly as before, and -- new here -- a clean
    # run that nonetheless skipped requested antenna work (e.g. a full sky130
    # stack whose met3-5 roles have no limit in
    # `_SKY130_ANTENNA_RATIO_MAX_EGAR`) now reports `"clean_partial"` rather
    # than the unconditional `"clean"` it used to: a partial result must
    # never be indistinguishable from a fully-graded one. Per-gate
    # `antenna_verdict` (`"pass_partial"`, #1997) is unchanged -- this only
    # changes the top-level roll-up across every gate.
    rollup = coverage_rollup(
        {"coverage": coverage}, failed=bool(erc_finding_count or any_antenna_violation)
    )
    status = rollup_status(rollup, success="clean", failure="violations")

    # `erc_status`/`erc_coverage` (issue #2179) -- the connectivity half's own
    # roll-up, beside the antenna-driven `status` rather than folded into it.
    #
    # `status` above is a roll-up of *both* questions, and its coverage input
    # is antenna-only, so on a PDK with no antenna-ratio table (every PDK but
    # sky130 today, or `--pdk` omitted entirely) it is `"not_checked"`/exit 4
    # no matter what the connectivity rules found: a run whose connectivity
    # rules all passed is then indistinguishable, by `status` and by exit
    # code, from a run that checked nothing at all. That is the right answer
    # *for the antenna question* -- so it is left exactly as it was -- but it
    # leaves the connectivity verdict, the only thing `klt erc` can report on
    # such a PDK, readable only by hand-rolling this same roll-up off
    # `erc_finding_count`. Every caller that wants the structural supply read
    # as a CI gate reimplemented it; this field is that roll-up, computed
    # once, here.
    #
    # Deliberately *not* a copy of `status` minus the coverage input: it is
    # graded on `erc_findings` alone, so an antenna violation on an unrelated
    # signal net never turns the connectivity read red (the same separation
    # `docs/design-evidence-tiers.md` item 11 already relies on -- "those are
    # the rules this item grades, not the report's overall `status`"). The
    # converse holds too: `status` still goes `"violations"` for a
    # connectivity finding, so nothing here weakens the combined verdict.
    #
    # A degenerate `ties[]` declaration (issue #2199) lands in that scope's
    # `skipped` list rather than `checked`, so this roll-up reports
    # `"clean_partial"` for it: an `erc.missing_tie` check that cannot tell
    # a tap from a source/drain contact must not be readable as the clean
    # missing-tie verdict `docs/design-evidence-tiers.md` item 11 asks for.
    erc_coverage = _connectivity_coverage(
        gates,
        nets_decl,
        ties,
        degenerate_ties,
        asserted_ties,
        ties_disclosure,
        well_asserted_ties,
    )
    erc_rollup = coverage_rollup(
        {"coverage": erc_coverage}, failed=bool(erc_finding_count)
    )
    erc_status = rollup_status(erc_rollup, success="clean", failure="violations")

    # `provenance.pdk` (issue #1968): `--pdk` here selects a built-in
    # antenna-ratio limit table baked into this module (see
    # `_ANTENNA_LIMITS_BY_PDK`), not an installed PDK directory resolved via
    # `klayout_tools.pdk.find_pdk` the way `klt drc`/`klt extract`/`klt lvs`
    # populate `provenance.pdk` -- there is no `--pdk-root` for `klt erc` to
    # resolve against. So this builds the same `{variant, resolved_via,
    # version}` shape `build_provenance` expects by hand, with
    # `resolved_via: "built-in"` naming that distinction honestly rather
    # than fabricating a filesystem resolution that never happened.
    # `None` when `--pdk` was omitted, matching every other verb's
    # conditional `pdk`/`deck` population.
    provenance_pdk = (
        {"variant": pdk, "resolved_via": "built-in", "version": None}
        if pdk is not None
        else None
    )

    # `provenance.deck` (issue #2204): `--deck` names a curated extraction
    # deck (see `_resolve_deck`), so this is populated exactly the way every
    # other `--deck`-taking verb populates it (`deck_name`/`deck_path` -->
    # `build_provenance`'s own `_deck_block`), unlike `provenance.pdk`
    # above, which is hand-built because `klt erc` resolves no filesystem
    # PDK at all. `None` when `--deck` was omitted, matching every other
    # verb's conditional `deck` population -- and this module's own
    # byte-identical-when-unused guarantee for this feature.
    provenance = build_provenance(
        deck_name=deck,
        deck_path=deck_source_path(deck) if deck is not None else None,
        pdk=provenance_pdk,
        input_path=file,
    )

    # `provenance.spec` (issue #2036): `klt erc` is validated against *two*
    # inputs, not one -- the layout (`provenance.input`) and the stackup/
    # vias/nets/ties spec. An ERC verdict is only meaningful relative to the
    # declarations it was run against, so a report that pins the layout but
    # not the spec still can't be re-verified: the spec can be edited (a
    # dropped `ties` entry, a re-pointed `layer`) and a committed report goes
    # on asserting a verdict for declarations it never saw. Attached here
    # rather than via a second `build_provenance` parameter, following `klt
    # lvs`'s precedent (`environment.reference_sha256`, `lvs.py`) of keeping
    # a verb-specific second-input hash out of the shared helper's signature.
    # `_content_hash` (not the bare `sha256_file`) so this reads identically
    # to the `sha256:`-prefixed `provenance.input.content_hash` beside it.
    provenance["spec"] = {"content_hash": _content_hash(spec_path)}

    # `provenance.devices` (issue #2183): what the `devices[]` carve-out
    # actually removed from the connectivity graph this verdict was computed
    # on. A subtraction that changes which nets exist has to be visible in
    # the report -- otherwise two runs of the same layout, one with a
    # `devices[]` declaration and one without, disagree about
    # `erc.supply_short` with nothing in either payload to say why. Each
    # entry carries the *measured* `body_area_um2` -- the marker
    # intersected with its role's own conductor region (issue #2226), not
    # the marker layer's own area -- so a declaration that silently
    # subtracted nothing (wrong datatype, wrong `on` role, marker layer
    # absent from this stream) is distinguishable from one that bit. Always
    # present; `[]` when no `devices` were declared and no `--deck` was
    # selected.
    #
    # Issue #2204: when `--deck` selects a curated deck, every entry
    # (hand-declared and deck-detected alike) additionally carries
    # `"source"` (`"declared"` vs `"deck"`) and `"superseded_by"` (the
    # declared device name that pre-empted a deck-detected carve-out for the
    # same role, `None` otherwise) -- see `_deck_device_cuts`. Both keys are
    # omitted entirely when no `--deck` was given, so a caller who never
    # opts into this feature sees byte-identical `provenance.devices`
    # entries to before this issue.
    provenance["devices"] = devices_applied

    return {
        "schema_version": SCHEMA_VERSION,
        "file": file,
        "spec": spec_path,
        "pdk": pdk,
        # (issue #2219) Whether the per-gate antenna accumulation was
        # skipped. A reader that finds `levels[].step_area_um2 == null`
        # must be able to tell "this run declined to measure it" from a
        # malformed report, without inferring it from `coverage.skipped`.
        "findings_only": findings_only,
        "gate_role": gate_role,
        "gate_count": len(gates),
        "gates": gates,
        "erc_findings": erc_findings,
        "erc_finding_count": erc_finding_count,
        "erc_status": erc_status,
        "status": status,
        "coverage": coverage,
        "erc_coverage": erc_coverage,
        # (issues #2234, #2247) The spec's top-level `ties_disclosure`,
        # echoed verbatim -- `None` when the spec did not declare one,
        # matching every other optional-spec-key echo's conditional
        # population, and carrying `kind` only when the spec itself did. See
        # `_validate_ties_disclosure` and this module's docstring, "Missing
        # substrate/well tie".
        "ties_disclosure": ties_disclosure,
        "provenance": provenance,
    }
