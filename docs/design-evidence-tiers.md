# Design-evidence tiers

A block's maturity is a claim about **evidence, not effort**: what has been
demonstrated, with which tools, and by whom. This doc defines a four-tier
evidence ladder for blocks designed with this toolkit, and the concrete
artifact checklist a block repo must satisfy at each tier. Tiers are graded
**per block per PDK** — the same design can sit at different tiers on
different nodes.

The ladder exists so that "this block is done" is never a vibe. A tier claim
is checkable: every requirement below names an artifact that either exists,
is fresh, and passes — or doesn't.

> **This file is runtime tool data, not only prose.** `klt signoff
> --manifest`/`--fleet` parse the ladder table and the T1 checklist below
> mechanically (`klayout_tools/design_evidence_tiers.py`), and every built
> wheel bundles a copy of this file as package data so a packaged install can
> read it without a source checkout (issue #1050). Restructuring either
> section — not merely rewording it — changes what `klt signoff` reports; see
> [`docs/cli/signoff.md`](cli/signoff.md)'s "Where the tier doc comes from".

## The ladder

| Tier | Claim | Demonstrated by |
|---|---|---|
| **T1 — sim-validated** | Designed and simulation-validated with open tools | Open-source DRC/LVS/simulation evidence, complete and passing (checklist below) |
| **T2 — signoff-validated** | Validated on commercial signoff tools | T1, plus DRC/LVS signoff and simulation on commercial tools with the foundry's own decks |
| **T3 — silicon-validated** | Fabricated and measured | T2, plus a tapeout on that PDK with measured parts and a published sim-to-measurement correlation |
| **T4 — production-validated** | Proven in an external user's silicon | An external project containing the block reached working silicon — only a user of the block can demonstrate this |

This toolkit's closed loop targets T1; T2+ require tools and fab access
outside its scope, and are defined here only so T1 claims are honest about
what they are not.

### Relationship to the everyblock catalog

This ladder is the same four rungs as the `2AMLogic/product` repo's everyblock
catalog, under commercial vocabulary:

| this repo | everyblock catalog | claim |
|---|---|---|
| T1 | bronze | sim-validated (open tools) |
| T2 | silver | signoff-validated (commercial decks) |
| T3 | gold | silicon-validated (measured) |
| T4 | platinum | production-validated (external user's silicon) |

`product/everyblock/tiers.md` is the commercial-side definition of the ladder;
`product/everyblock/grants.md` is the authoritative record of whether a given
block is actually *at* bronze/silver/gold/platinum on a given PDK. The T1
checklist below is the concrete, checkable expansion of the catalog's
one-line "bronze" evidence column — engineering tracks progress against it in
each canary's "gap to T1" issue, but this repo's checklist does not itself
grant a tier; `grants.md` does. Whether the internal (T1-T4) and external
(bronze/silver/gold/platinum) vocabularies stay split by audience, or
converge on one name, is a proposed-but-pending operator decision recorded as
OPEN in `product/everyblock/tiers.md`.

## T1 checklist — what "sim-validated" requires

Every item names the artifact and the pass condition. "Fresh" means the
artifact's provenance (input hashes, netlist/layout revision) matches the
block's current sources — a passing report against last week's netlist is
evidence of nothing.

**Not every item has a tool behind it.** Items 3-7 each name a `klt` verb
that can mechanically grade them, and item 8 has a purpose-built generic
evidence envelope — but items **1**, **2**, **9**, and **10** have none.
`klt signoff --manifest` therefore grades those four on whether *some*
passing envelope was cited at all, not on whether the cited evidence is
topically relevant to the claim; see
[`docs/cli/signoff.md`](cli/signoff.md)'s "Items 1, 2, 9, and 10: `klt
signoff` cannot check topical relevance". Citing them honestly is the
claimant's responsibility, not something the tool verifies.

### What a T1 claim is scoped to

**T1 is block-scoped.** Every item is evidence about *this block*, at its
own boundary, on its own. Nothing in the checklist says anything about the
chip the block is later integrated into, or about conditions imposed on the
block from outside that boundary — supply quality at its pins, the
integrating design's floorplan, a neighbour's injected noise. A block at T1
is a block whose own artifacts are complete, fresh, and passing; it is not a
certificate that a design containing it will work.

**T1 is toolchain-scoped.** Each item is graded by the open-source tools it
names, at those tools' actual current coverage. An item is satisfied when
those tools report clean — which is not the same claim as "no defect of this
class exists in this block". A deck with rule-free layers (item 3), a
compare whose `power_connectivity` came back `unchecked` (item 4), a corner
set the cited run itself declared (item 5): every verdict is bounded by the
question the tool actually asked. **A defect class that no `klt` verb
detects — or that a verb detects but `klt signoff` cannot read as a failing
check — is outside every T1 item by construction, and a T1 claim asserts
nothing about it.** This is why "Coverage honesty" is a verification rule
below, and why items 3 and 4 require their coverage gaps to be disclosed
*with* the claim rather than folded silently into a pass.

### Block kind

A T1 claim states the block's **kind**: `analog`, `digital`, or
`mixed-signal`. The kind determines which column of items 1, 2, 5, 7, and 11
applies — items 3, 4, 6, 8, 9, and 10 are kind-independent and apply as
written to every block.

- **Analog** blocks satisfy the *Analog* column only.
- **Digital** blocks satisfy the *Digital* column only. Nothing in the
  Analog column (schematic capture, PVT corner sweeps, Monte Carlo) is
  applicable or required — a digital block is not held to analog artifacts
  it has no reason to produce.
  - **Full-custom digital sub-case.** A digital partition is not always
    RTL/synthesis by construction: where no compatible open standard-cell
    library exists for the PDK/voltage combination in use, every logic gate
    may instead be hand-captured as a schematic and verified via SPICE + PVT
    sweep, exactly like an analog partition — no RTL, no synthesis step.
    Items 1, 2, and 5 below spell out the full-custom substitute artifact
    inline, as a **sub-case of the Digital column** rather than a third
    column or a new block `kind` (issue #1190) — the same choice #636
    made for the Analog/Digital split itself stays the load-bearing
    structure, so neither `klt signoff`'s parser
    (`design_evidence_tiers.py`'s `_COLUMN_BULLET`, which hardcodes exactly
    the two labels `Analog`/`Digital`) nor its manifest `kind` enum
    (`analog`/`digital`/`mixed-signal`, `signoff.py`'s `_BLOCK_KINDS`) needs
    to change — a full-custom digital block still declares `kind: "digital"`
    and is graded the same way any other digital block is. Item 7 needs no
    separate full-custom text either: a digital block's item 7 accepts a
    `klt pex` citation *or* the RTL-flow artifact the Digital column itself
    names (issue #1959 — see
    [`docs/cli/signoff.md`](cli/signoff.md)'s "Item 7 is kind-restricted"
    section), so a full-custom partition's own post-layout re-simulation
    against its drawn layout satisfies it exactly like the Analog column's
    item 7 does, with no full-custom-specific wording needed here.
- **Mixed-signal** blocks partition into analog and digital sub-blocks
  within the same repo and satisfy **both** columns, one per partition. The
  claim must state the partition boundary explicitly (which nets/pins/cells
  belong to which side) so a reviewer can tell which evidence covers which
  silicon.

1. **Design sources**
   - *Analog* — committed schematic sources (or generator) plus the
     netlist derived from them, regenerated on design change.
   - *Digital* — committed RTL sources plus the synthesized gate-level
     netlist derived from them, regenerated on design change. For a
     hand-captured full-custom digital partition (no RTL, no synthesis
     step — see "Full-custom digital sub-case" above), this item is
     satisfied instead by the Analog column's artifact: committed
     schematic sources (or generator) plus the netlist derived from them,
     regenerated on design change, with every logic gate captured as a
     schematic exactly like an analog partition's.
2. **Layout**
   - *Analog* — committed GDS/OASIS, reproducibly generated or with
     documented provenance.
   - *Digital* — committed routed GDS/OASIS from place-and-route,
     reproducibly generated from the gate-level netlist (P&R script/flow
     committed, not a one-off hand edit). For the same full-custom
     sub-case, this item is satisfied instead by the Analog column's
     artifact: committed GDS/OASIS, reproducibly generated or with
     documented provenance — hand-drawn layout, not P&R output.
3. **DRC clean** — latest `klt drc` JSON report: `status: clean`, fresh,
   with the deck identified (content hash). Known deck coverage gaps
   (rule-free layers, skipped rules) must be enumerated in the claim, not
   hidden behind "clean" — a clean verdict from a deck with undisclosed
   holes is a false claim. The report already states its own gaps, so the
   disclosure is those fields quoted from the cited envelope's `coverage`
   block (`docs/cli/drc.md`), not prose written from memory:
   `coverage.layers_in_stream_without_rules` (layers drawn in this stream
   that the deck has no rule for), `coverage.rules_skipped` (rules the deck
   carries but this run did not evaluate), and `coverage.deck_scope` (which
   chapters of the foundry DRM the deck transcribes at all). A claim that
   leaves a non-empty `layers_in_stream_without_rules` or `rules_skipped`
   unstated, or that never states the `deck_scope` its "clean" was measured
   inside, has not satisfied this item however clean the `status` is.
   **This disclosure is claimant-enforced, not tool-enforced**: `klt
   signoff` grades item 3 on `status: "clean"` alone — a deck with rule-free
   drawn layers or skipped rules grades `met` exactly like a fully-covering
   one. Since #2002 it does *report* all three fields (item 3's citation
   carries a `coverage` block quoting them verbatim, `docs/cli/signoff.md` →
   "DRC coverage is reported, not graded"), so a reviewer no longer has to
   re-open the DRC envelope to find them — but reporting is not enforcing:
   nothing compares them against what the claim actually disclosed, so a
   `met` verdict from `klt signoff` is still not evidence that the gaps were
   disclosed. A reviewer must read the reported `coverage` against the claim
   themselves.
4. **LVS clean** — latest LVS report `status: match`, fresh, engine named,
   checked against the netlist from item 1 (schematic netlist for analog,
   synthesized/routed gate-level netlist for digital), **and that same
   report's `power_connectivity.status` not `"mismatch"`**. Those are two
   verdicts, not one: `klt lvs`'s top-level `status` is the *signal*-
   connectivity compare's own result, and since issue #1952 the
   power/ground half is reported beside it in its own `power_connectivity`
   block (`docs/cli/lvs.md` → "Power/ground connectivity"). `klt signoff`
   already grades this item on both — an `lvs`-kind check passes only on
   `status == "match"` **and** `power_connectivity.status != "mismatch"`
   (issue #1965) — so a claim resting on `status: match` alone is weaker
   than the tool that grades it. What the check verifies is
   per-standard-cell-instance *pin-to-net* correctness: on every abstracted
   instance, each pin the PDK library declares power/ground must reach the
   net the caller declared (`options.power_connectivity.expected_nets`) or,
   absent a declaration, the same net every other instance's same-named pin
   reaches; a disagreement renders `power.inconsistent_pin_net` /
   `power.unexpected_pin_net`, and a pin on no net at all renders
   `power.unconnected_pin`. **The check's reach is narrower than "the power
   grid is verified", and a claim must not say otherwise.** A
   `power_connectivity.status` of `"unchecked"` — every `reference.form`
   other than `gate-level-verilog`, an explicit `options.power_connectivity:
   false`, or a layout whose instances declare no PG pins at all — means the
   question was never asked; this item and `klt signoff` both read it as
   "does not apply here", never as "verified", so a fleet policy of "re-run
   LVS, look for `match`" is not by itself power-connectivity evidence
   (#1985). And even where it does run it is *not* a geometric rail/grid
   continuity check: whether a rail is an unbroken annulus, and whether the
   grid carries the current, stay `klt ring-check`, `klt power`, and the DRC
   deck's business — see "Power/IR-drop + EM evidence" below for what is and
   is not covered. Warnings-only mismatches must be listed with the claim.
   A second, independent engine's concurring verdict (#343) strengthens this
   from "one toolchain agrees with itself" to a cross-checked result.
5. **Full corner verification vs a ratified spec**
   - *Analog* — PVT corner-matrix simulation results covering every spec
     row at its bound corners, with per-row pass/fail and the binding
     corner recorded.
   - *Digital* — multi-corner static timing analysis (setup and hold
     across the PVT corner set) plus a bit-exact functional test suite,
     with per-corner and per-test pass/fail recorded. For the full-custom
     sub-case, this item is satisfied instead by PVT corner-matrix SPICE
     simulation covering every spec row (the Analog column's artifact),
     plus an explicit per-corner timing-margin metric measured directly in
     that same sweep — a SPICE-measured analog of STA setup/hold margin,
     e.g. a derived arrival-time-vs-required-time delta recorded per PVT
     point — as the full-custom substitute for the STA requirement above.
     The machine-checkable evidence for the RTL-flow artifacts is a `klt
     sta` JSON report run across the declared corner set (`pdk.corners`,
     `docs/cli/sta.md`) and a `klt functional-verification` JSON report
     (`docs/cli/functional-verification.md`); `klt signoff`'s tier-verdict
     mode recognises both as first-class evidence kinds (#1959), so a
     digital block no longer has to produce a `klt sim` corner sweep it has
     no reason to run. An `sta` citation counts as passing only when every
     corner it reports is `timing_status: "constrained"` with non-negative
     setup and hold slack — the corner set graded is the one the cited run
     itself declared.
   - Both require the spec table itself to be ratified — verdicts against
     a draft spec are provisional by construction.
6. **Statistical claims carry Monte Carlo evidence** — any accuracy,
   offset, or matching spec row is statistical; a corner matrix (or STA
   corner sweep) cannot validate it. This applies to whichever spec rows
   are actually statistical, regardless of block kind — most digital spec
   rows (functional correctness, Fmax, timing closure) are not, and a
   block whose spec has no statistical row must say so explicitly rather
   than omit the item. Where it does apply, MC runs need a recorded seed,
   sample count, a deterministic negative control, and results combined
   with (not instead of) process corners (#344). A `klt yield` JSON report
   — a yield estimate with its confidence interval, sample-size verdict, and
   Cpk/sigma-to-spec against the row's own limits — is the machine-checkable
   evidence for this item (`klt signoff`'s tier-verdict mode grades it the
   same way it grades the deterministic items above).
7. **Post-layout verification**
   - *Analog* — the spec suite re-run against the netlist extracted from
     the layout, not only the drawn schematic (#252). Parasitic extraction
     has landed (#217, `klt extract --parasitics`); a `klt pex` report's
     `extraction.model` field is the machine-checkable statement of what the
     extracted netlist's lumped-RC model does and does not account for
     (quasi-static, vertical-overlap coupling only, issue #760 — see
     `docs/cli/pex.md` → "Top-level fields"). A `klt pex` JSON report — the
     per-corner, per-spec-row schematic-vs-extracted delta — is the
     machine-checkable evidence for this item, and the *only* evidence `klt
     signoff`'s tier-verdict mode accepts for it: unlike every other item,
     item 7 rejects a passing citation of any other kind, since a clean DRC
     or a pre-layout schematic sim proves nothing about post-layout
     behaviour (#871). `klt pex` (Epic #709, issue #801) is implemented —
     see `docs/cli/pex.md` for its full contract and `docs/cli/signoff.md`'s
     "Item 7 is kind-restricted" section for how `klt signoff` binds to it.
     Epic #709 Phase 1c (#803) is the first end-to-end proof of this item on
     a real analog canary (`blocks/sky130-ota-5t` /
     `examples/design-pipeline/`, see that directory's README "S10 pex delta
     proof" section) — every `delta[]` row is explainable directly from the
     extracted netlist's own added R/C, not merely reported, including the
     (measured, not assumed) no-degradation rows that canary's current
     topology produces.
   - *Digital* — the functional test suite re-run against the post-route
     gate-level netlist with back-annotated SDF timing, not only the
     pre-layout RTL/gate simulation. The machine-checkable evidence is a
     `klt functional-verification` JSON report from an SDF-annotated run
     (`options.sdf`, `docs/cli/functional-verification.md`) — its
     `environment.sdf` block is `null` on an ordinary zero-delay run and an
     object with `annotated: true` on an annotated one, so the two are never
     mistakable for each other from the JSON alone. `klt signoff` accepts
     exactly that report, or a `klt pex` report (the full-custom sub-case's
     own post-layout artifact), for a digital block's item 7 (#1959); an
     unannotated regression renders `not_post_layout`, and a `klt sta` run —
     even a SPEF-annotated one — is item 5's timing evidence, not this
     item's functional re-simulation, so it renders `wrong_kind`.
   - A `klt pex` citation is only as good as the **device-body bias** of the
     netlist it ran on (#1983). A device body left on an anonymous,
     deck-synthesized net has no DC bias path at all, which makes a
     resimulation of that extracted netlist *physically wrong, not merely
     imprecise* (`docs/cli/extract.md` → "Coverage") — the run converges and
     reports numbers, and those numbers are not comparable to the schematic
     leg they are being diffed against. A `klt pex` report states the
     condition in its own `body_bias` block (`docs/cli/pex.md`), and `klt
     signoff` surfaces it verbatim on this item's citation
     (`citation.body_bias`). **It does not grade on it**: an item-7 citation
     whose `body_bias.status` is `"unbiased"` still renders `met`, because
     the condition is a deck-coverage property some PDKs produce on every
     layout they extract, not a defect this command can adjudicate. So a
     claim citing item 7 must read that field and state whether its
     post-layout numbers were measured on a properly-biased netlist —
     claimant-enforced, exactly like item 3's DRC-coverage disclosure. The
     same discipline applies upstream at item 4: `klt lvs`'s
     `body_verification` block says whether the compare verified the body
     ties at all, and `"unchecked"` (a pre-extracted netlist) is not
     `"verified"`.
8. **Characterization report** — one aggregated, current artifact
   summarizing per-spec-row performance across conditions, with the
   evidence record each verdict rests on (#309 tracks the aggregation
   tool). For a digital or mixed-signal digital partition this includes
   Fmax, area, and power across the corner set, not just functional
   pass/fail. Unlike every other T1 item, this one names no specific `klt`
   verb — there is no dedicated aggregation command, and its evidence may be
   a hand-assembled record (e.g. a committed Markdown characterization
   report) rather than one `klt` verb's own JSON output. `klt signoff
   --manifest` grades it via an opt-in **generic evidence envelope**
   (`"kind": "generic"`, issue #1152) — a minimal, hand-rolled JSON wrapper
   asserting `status: "pass"|"fail"` for whatever record backs it — and,
   unlike every other item, item 8 is the *only* T1 item this generic kind
   may satisfy: see `docs/cli/signoff.md`'s "Generic evidence (opt-in,
   non-`klt`-native)" section for the envelope shape and why items 3–7 keep
   rejecting it.
9. **Testbenches shipped** — every claimed measurement's testbench
   committed, with a documented cold-start invocation a third party can
   run; pinned PDK revision.
10. **Repo hygiene** — a README stating what the block is, its spec table,
    and how to reproduce every result; a license; CI that at minimum
    keeps the harness and evidence formats valid.
11. **Power delivery (structural)**
    - *Analog* — the `klt erc` supply evidence below, plus an LVS report
      (item 4's own) whose reference actually carried the supply nets, so
      the supplies were part of the compare rather than absent from it:
      every declared supply net appears in that report's
      `net_correspondence` paired to a reference-side net. A SPICE
      reference satisfies this by construction; a signal-only
      `gate-level-verilog` reference does not, which is why the Digital
      column asks for `power_connectivity` instead.
    - *Digital* — the `klt erc` supply evidence below, plus the routed
      artifact having actually been produced with a power grid, plus item
      4's power/ground verdict having actually run. Concretely: the `klt
      place-and-route` response's `power.pdn` is `true` with a
      `power.tapcell_master` named (`docs/cli/place-and-route.md` → "Power
      delivery"), every `power.straps[].layer` is covered by the cited
      `klt erc` spec's own stackup, and that same LVS report's
      `power_connectivity.status` is `"match"` — `"unchecked"` does **not**
      satisfy this item, unlike item 4 itself, where it means "the question
      does not apply here". For a hand-captured full-custom digital
      partition (no RTL, no synthesis, no P&R — see "Full-custom digital
      sub-case" above), this item is satisfied instead by the Analog
      column's artifacts, exactly as items 1, 2, and 5 are: there is no
      `klt place-and-route` response to cite, and the hand-drawn block's
      SPICE-reference LVS already carries its supplies.
    - Both columns rest on the same `klt erc` **supply spec** run
      (`docs/cli/erc.md`): a spec declaring every supply as a `nets[]`
      entry with `"kind": "supply"`, a stackup covering the layers the
      supply is routed on, and the `ties[]` declarations for the
      well/substrate taps. Every declared supply must resolve to exactly
      **one** electrical island — no `erc.unconnected_net` and no
      `erc.supply_short` naming it — and the run must report zero
      `erc.missing_tie`, from a tie the run actually *checked*: a `ties[]`
      entry `klt erc` reports as degenerate (`erc_coverage.skipped[]`,
      reason `degenerate_tap_declaration`, issue #2199) returns zero for a
      reason that has nothing to do with taps, and renders
      `supply_spec_incomplete` rather than a met item — the same rule that
      already rejects a spec declaring no `ties[]` at all. Those are the
      rules this item grades, not the
      report's overall `status`: an antenna verdict or a floating-gate
      finding is a real defect, but it is not this item's subject and does
      not block it (#1994). IR-drop and EM (`klt power`) stay deliberately
      **outside** this item — see "Power/IR-drop + EM evidence" below. This
      item is the *structural* question ("is the supply connected to what it
      powers"), not the *analysis* question ("how far does it droop, and
      does any segment exceed its EM limit"). **Caveat (issue #2180)**:
      `erc.unconnected_net` can be a false positive for a declared supply
      whose real continuity in silicon runs through diffusion/well material
      outside the declared `stackup` — see `docs/cli/erc.md`'s "Known
      false-positive: diffusion/well continuity is not modeled" section. A
      grader who sees a multi-island `erc.unconnected_net` finding on a
      guard-ring/well-tap-strapped supply should not treat it as a confirmed
      power-delivery defect on its own — cross-check against an
      independent, device-aware LVS run first (and confirm that LVS deck's
      own connectivity setup does not join nets by label alone, e.g. via a
      bare `connect_implicit('*')` rule, before trusting a "match" verdict
      as that independent check). **Caveat (issue #2183)**: the mirror-image
      false positive applies to `erc.supply_short`. `klt erc` traces
      conductor geometry with no device recognition, so a drawn device body
      on a declared role — a poly resistor, a poly fuse, a MiM/MOM
      capacitor plate — reads as a wire. A block that deliberately spans
      its two supplies through such a device (a power-on-reset divider, a
      brown-out detector, a supply-referenced bias string) therefore
      reports an `erc.supply_short` that is an artifact of the declaration,
      not a power-delivery defect. **Declare those bodies in the spec's
      `devices[]` array** — see `docs/cli/erc.md`'s "Device bodies are not
      wires" section — and the item becomes gradeable for such a block. A
      grader seeing an `erc.supply_short` on a supply-sensing analog block
      should check the cited report's `provenance.devices` (empty, or
      missing the bodies on the conducting path) before reading the finding
      as a real short. **`--deck` (issue #2204) removes the need for
      hand-declaration for a device a curated extraction deck already knows
      about**: running the same supply-spec check with `--deck <name>`
      (e.g. `gf180mcu`/`sky130`) auto-carves out every deck-recognised
      `ResistorDevice`/`CapacitorDevice` body whose conducting-body layer
      matches a declared `stackup`/`vias` role, echoed in
      `provenance.devices` exactly as a hand-declared entry is (plus
      `source: "deck"`) — see `docs/cli/erc.md`'s "Deck-driven
      device-marker auto-detection". A run without `--deck`, or whose
      device is not yet covered by any curated deck's `resistors`/
      `capacitors` tables, still needs the explicit `devices[]` declaration
      above; a grader should check `provenance.deck` alongside
      `provenance.devices` before treating a clean `erc.supply_short` read
      as covered by auto-detection rather than a hand-declaration that
      happens to be present.

## Power/IR-drop + EM evidence (not yet a T1 item)

**Power delivery asks two questions, and only one of them is deferred
here.** There is an *analysis* question — how far does the supply droop
under load, and does any segment exceed its EM current-density limit — and a
*structural* question — is the supply actually connected to what it powers.
This section defers the analysis question only. An earlier revision of it
called power-grid evidence "orthogonal to the DRC/LVS/corner/Monte-Carlo/
post-layout checklist"; that was true of the analysis question and wrong
about the structural one, which is item 11's own subject matter (and, for
per-cell-instance pin-to-net correctness, item 4's — `power_connectivity`,
issues #1952/#1965). Read nothing in this section as deferring the
structural question.

`klt power` (Epic #712, issues #844/#845/#846 —
[`docs/cli/power.md`](cli/power.md)) reports a routed design's static
IR-drop map (`worst_case_droop_mv`) and a per-net electromigration (EM)
current-density verdict (`em_verdict`). **No T1 item above sets an IR-drop
or EM bound**, and extending the itemized T1 list to add one is a separate,
larger decision this doc does not make yet. Note that no new item is needed
for a block whose *own ratified spec* carries a droop or current-density
row: item 5 ("every spec row, per-row pass/fail") already makes such a row
binding through machinery that exists — the same mechanism the
"Area-efficiency spec convention" section below relies on.

**What the structural question is covered by, and how that changed.** Item
4's `power_connectivity` covers per-cell-instance pin-to-net correctness, and
only for a `gate-level-verilog` compare (see item 4 for its exact reach and
for what `"unchecked"` does and does not mean). It does not cover geometric
rail/grid continuity, and on its own it never required a power grid to exist
at all: a routed digital block with zero straps and zero PDN vias was caught
only to the extent that verdict caught it — which is not at all when it is
`"unchecked"`, and never on the grounds of the missing grid itself. Whether
the checklist should gain an item requiring a connected power delivery
network was a live question this doc deliberately did not settle, because
adding one changes `t1_item_count` and therefore what every existing T1
claim means, which made it an operator decision rather than a tooling one
(#1982). **That decision was taken — approved 2026-09-17 (#2025) — and is
item 11 above.** The structural question is now graded directly, per block
kind, from a `klt erc` supply-spec run (plus, for an RTL-flow digital block,
the `klt place-and-route` response's own `power` block and item 4's
`power_connectivity` verdict). Item 4 continues to mean exactly what it
always meant; item 11 is what a claim's "the supply reaches what it powers"
statement now rests on.

`klt signoff` (issue #1321, Phase 2 of epic #712) recognises a `klt power`
JSON envelope as a `"power"`-kind check **in envelope-aggregation mode
only** (`klt signoff drc.json ... power.json`) — passing on
`em_verdict.status: "pass"` — so a `klt power` run at least counts toward a
signoff bundle's own pass/fail verdict even though it satisfies no T1
checklist item yet. `--manifest`/`--fleet` tier-verdict mode does not accept
a `"power"`-kind citation for any item (including item 8): see
[`docs/cli/signoff.md`](cli/signoff.md)'s "No T1 item accepts `power`
evidence" section.

## Area-efficiency spec convention (AREA-EFF)

An absolute area bound alone cannot tell **area a block needs** from **area
a block wastes** — a block can pass every T1 item while sprawling (the
premise `klt economy`, issue #1012, was built on: agent-produced layouts
are correct-but-sprawling by default, and area is unit cost). This section
defines a second, companion spec row that answers the efficiency question
an `Area` row cannot, so a decision to loosen an area bound can turn on
*whether the area is well spent*, not only on how much of it there is.

### Two spec rows per block

- **`Area`** — absolute bbox area ≤ X mm². Ratified, customer-facing,
  revisable through the normal DR process. This is the row canary READMEs
  already carry (see "Where a ratified spec lives" below).
- **`Area-Eff`** — layout efficiency: a small composite metric backed
  entirely by fields `klt economy` (issue #1012, `docs/cli/economy.md`)
  already reports, checked in one command via `klt economy`'s
  `--area-eff-*` flags (`docs/cli/economy.md`'s "AREA-EFF bounds-check
  block" section documents the exact request/response shape):
  - **Hard bounds on unambiguous waste** — no analog-legitimacy defense
    (guard ring, matching symmetry, isolation spacing) applies to any of
    these:
    - `dead_margins_um` (`--area-eff-max-dead-margin-um`) — a per-edge cap
      on empty bands at the block's own bbox edges.
    - `bbox_tightness` (`--area-eff-require-bbox-tightness`) — must equal
      `1.0`: the bbox must not extend past the drawn content at all.
    - `largest_empty_regions` (`--area-eff-max-empty-region-fraction`) — no
      single disjoint empty region above a set fraction of the bbox area.
  - **Calibrated bound**: `utilization` (`--area-eff-min-utilization`) ≥ a
    per-block-kind floor. Unlike the checks above, this one is *not* a
    fixed rule for every block — see "Hard bounds vs. a calibrated bound"
    below.
  - **Where a comparable design exists**: `klt economy`'s existing
    `--reference-area-um2` ratio (a general area comparison, not
    AREA-EFF-specific) can additionally be cited against a hand-designed
    reference.
  - **Judgment layer**: an `economy-review` skill
    (`.claude/skills/economy-review/SKILL.md`) verdict of `pass` — the
    rubric distinguishes guard rings / matching symmetry / isolation
    spacing from genuine waste, the reason the metrics above can't stand
    alone as automatic gates.

### Hard bounds vs. a calibrated bound

**Goodhart cuts both ways here.** A utilization floor alone pressures
cramming, which trades against matching and DRC margin — the opposite
failure from sprawl. That is why only the unambiguous-waste metrics
(`dead_margins_um`, `bbox_tightness`, `largest_empty_regions`) get hard,
one-size-fits-every-block bounds, while `utilization` gets a calibrated,
block-kind-dependent floor with the `economy-review` skill's rubric
verdict as the judgment layer that catches what a bare number can't (a
floorplan that is tight everywhere but shaped wrong, or legitimately
sparse for a documented reason).

Per-block-kind utilization floors are seeded from the `economy-review`
skill's own rubric table (`.claude/skills/economy-review/SKILL.md`,
calibrated against two real canaries — see "Calibration evidence" below),
not invented here. Only the digital row carries a named hard floor today;
the analog/mixed-signal rows carry a typical range (and, for matched
pairs, a soft "flag, not automatic fail" threshold) rather than a single
number, precisely because a wrong floor there is the cramming-vs-matching
Goodhart failure this section exists to avoid — an `Area-Eff` row for one
of those kinds should set `--area-eff-min-utilization` from the *typical
range's own low end* only when the block's own evidence (an `economy-review`
verdict, or prior fab data) supports it, not by default:

| Block kind | `--area-eff-min-utilization` | Typical range |
|---|---|---|
| Digital standard-cell rows | 0.70 (named floor) | ≥ 0.85 typical |
| Analog matched pairs / current mirrors | no named floor — below ~0.25 is a flag, not an automatic fail | 0.35–0.55 typical |
| Analog isolation-heavy (bandgap, LDO, references) | no named floor | 0.30–0.50 typical |
| Mixed-signal top-level integration | no named floor | 0.40–0.65 typical |

A ratified `Area`-row budget always overrides these ranges when one exists
— they are a starting point for setting an `Area-Eff` row's own bound, not
a substitute for one. Do not restate the derivation of any of these ranges
here; the `economy-review` skill's `SKILL.md` is their source of truth and
this table only mirrors it for convenience at the point an `Area-Eff` row
is being drafted.

### Calibration evidence

The floors above were cross-checked against real `klt economy` output
(issue #1086), not only the placeholder script that originally seeded
`SKILL.md`'s table:

- `blocks/sky130_fd_sc_hd__buf_4` (digital standard cell, known-tight):
  `klt economy` reports `utilization: 0.9397`, `dead_margins_um` all zero,
  `bbox_tightness: 1.0` — comfortably clears the 0.70 digital floor and
  every hard bound. See `evidence/economy-review/sky130_fd_sc_hd__buf_4/`.
- `blocks/sky130-bandgap` (analog, isolation-heavy, known-loose — its own
  `NOTE.md` documents the area budget was relaxed to fit the drawn layout):
  `klt economy` reports `utilization: 0.3805` (inside the 0.30–0.50
  isolation-heavy range on its own) but `dead_margins_um` of ~41 um on both
  left and right edges — exactly the case an `Area-Eff` row's hard bounds
  exist for: utilization alone would not flag this block, but an
  unambiguous-waste bound does. See
  `evidence/economy-review/sky130-bandgap/`.

Both records' `klt economy` numbers matched the placeholder script's
numbers that originally seeded `SKILL.md`'s table (to full float
precision, same input content hash) — the floors above did not need
revision, only confirmation against the shipped tool.

### Where a ratified spec lives

There is no in-repo JSON/table schema this repo owns for spec rows —
ratified specs live in each block's own canary repo README under a
`## Target specification (...)` heading, parsed by
`scripts/ingest-canary.py`'s `parse_spec_summary()` into
`spec_summary.rows[]` (keyed by slugified column headers —
`parameter`/`target`/`stretch`/`corner_binding` for the two current
canaries, not hardcoded so a future canary can add/drop columns) plus a
`status_note` from the heading's parenthetical (e.g. `"RATIFIED
2026-07-31, see issue #1 and #35"`).

The existing row-name convention is `Area` (not `AREA`) — an `Area-Eff` row
follows the same casing for consistency. `parse_spec_summary()` reads
column headers verbatim and does not match `parameter` cell values against
a fixed enum, so an `Area-Eff` row parses with no code change.

### How this binds

Once an `Area-Eff` row is in a block's ratified spec, T1 checklist item 5
above ("full corner verification vs a ratified spec — every spec row, per-
row pass/fail") makes it binding through machinery that already exists —
**no change to the T1 checklist or the tier ladder is needed.** The
`economy-review` skill's stance is unchanged by this: it "renders opinions,
nothing here blocks a merge by itself" — `Area-Eff` binds through a block's
*ratified spec*, not through a new lifecycle gate.

### Relationship to matching-and-floorplanning.md

`docs/design/matching-and-floorplanning.md`'s "Relationship to the #1013
layout-economy rubric" section already derives the numeric relationship
between Pelgrom-law matching requirements and legitimate matching-driven
area — that derivation is not repeated here. This section only names the
spec-row convention and the CLI mechanism that checks it; the sizing-time
question of *how much* area a specific matched structure legitimately
needs stays owned by that document.

## Provenance hygiene in evidence records

Every item above rests on committed artifacts, and T1 item 9 ("pinned PDK
revision") and the staleness rule below both push a harness to record *what
environment produced this result*. That recording is permanent and public:
evidence records are append-only by construction, and a record id embeds the
commit SHA it was taken at — so a record cannot be rewritten later without
destroying the verifiability that is the reason the evidence is published at
all. Whatever a harness writes into a record, it writes forever.

**The rule: an evidence record identifies the environment, never the
author or the machine.** Concretely:

- **Repo-relative paths only.** A path inside the block repo is recorded
  relative to the repo root. A path *outside* it — a PDK install, a scratch
  directory, a home directory — is recorded as being outside, and its
  absolute location is not recorded at all. An external input is pinned by
  **identity**, not location: the PDK name/version and the deck
  `content_hash` the shared `provenance` block already carries
  (`docs/json-contract.md`) reproduce a run; where the PDK happens to sit on
  one machine reproduces nothing.
- **A stable pseudonymous host id, not a hostname.** Correlating two records
  to the same machine is legitimately useful ("both failures came from one
  host"); naming that machine is not. Record `host-<8hex>` — a salted hash of
  the hostname, the same opaque-id shape the fleet's own lease records use.
- **No login/author field.** Git already records authorship, once, where a
  reader expects to find it. A record that repeats it is adding a second,
  unrevocable copy of an identifier to a public artifact for no evidentiary
  gain.

This is not hypothetical hygiene. A 2026-08 disclosure read-audit of the
public canary repos found ~3,937 committed evidence records carrying all
three — an absolute `/Users/<author>/…` PDK path, the dispatch host's name,
and the author's login — written **by design** by each canary's own harness,
so the count grew with every simulation run.

**The reference implementation is `klt env-provenance`**
([`docs/cli/env-provenance.md`](cli/env-provenance.md)): `emit` produces
exactly this shape, and its Python module
(`klayout_tools.env_provenance`) is importable, so a harness calls it instead
of collecting host/path metadata itself. It **refuses to emit** a payload
carrying a local identifier rather than minting a record that leaks one, and
its `scan` subcommand reports home-directory-shaped absolute paths in files
that already exist, so a regression is caught while it is still a diff.

**Records that already leak are not rewritten.** Rewriting them would break
every record id and every citation resting on it — a strictly worse outcome
than the disclosure. The rule binds the writer, from now on.

## Verification rules

- **Staleness is failure.** Every report is checked against current source
  revisions/hashes before it counts. Provenance blocks in klt JSON output
  (#335) exist for this. `klt signoff --manifest` applies the rule in two
  steps, and discloses how far it got: the manifest's pinned `content_hash`
  is *gated* against the cited envelope's self-reported
  `provenance.input.content_hash` (a mismatch renders the item `unmet`),
  and that self-report is then checked against the **artifact itself** by
  re-hashing the input the envelope names — reported per citation as
  `input_verified: true | false | null` (issue #2196), never graded on. A
  `null` there means the freshness claim was only ever compared to another
  claim: two statements about a revision, neither of which is the revision.
  Committing evidence beside the artifact it names (so the path resolves
  from the repo) is what turns that `null` into a real check.
- **Coverage honesty.** A verdict's blind spots (deck holes, warning-level
  mismatches, uncombined evidence legs) are part of the claim and travel
  with it.
- **No claim without a testbench.** A spec row with no runnable bench
  checking it is unaddressed, whatever the prose says.
- **Downgrades are automatic.** Evidence going stale (design change without
  re-run) drops the block below the tier until re-established.

The `design-signoff` skill (`.claude/skills/design-signoff/`) turns this
checklist into a per-repo qualification report.
