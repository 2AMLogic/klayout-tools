# Study: custom transistor-level block layout (mixed-device rows, fixed pitch, constrained ports)

**Status:** design study / proposal for issue #3922. Nothing here is scheduled,
and nothing here approves implementation. Per
[docs/ARCHITECTURE.md](../ARCHITECTURE.md) → "How capabilities arrive," a new
capability is spiked first (survey, contract, build/wrap decision); this
document is that spike for the narrow gap the third friction report names. No
generator, router, CLI verb or deck rule is added by the PR that carries it,
and no simulation or EDA campaign was run to write it.

**Evidence convention.** Every claim is tagged one of three ways:

- **read** — read from a file in this checkout at `origin/main` @ `ae4a88b4c`
  (2026-10-20); nothing was executed.
- **reported** — stated by the downstream reporter in #3922 and **not**
  independently verified here (the reporter's block lives in a different repo).
- **prior-spike** — taken from an earlier spike in this repo, which fetched it
  live on the date that spike states; not re-fetched here.

Nothing in this document is "measured" in the sense of a command whose output
was captured. Where a number would need a run, section 5 says what run.

## Contents

1. [Capability map: what exists, what the gap actually is](#1-capability-map-what-exists-what-the-gap-actually-is)
2. [Recurring need and product value](#2-recurring-need-and-product-value)
3. [Survey: wrap, extend, or build](#3-survey-wrap-extend-or-build)
4. [Representative worked example](#4-representative-worked-example)
5. [Proposed contract and verification oracle](#5-proposed-contract-and-verification-oracle)
6. [Staged path, estimates, and go/no-go](#6-staged-path-estimates-and-gono-go)
7. [Related work reviewed (no duplicates)](#7-related-work-reviewed-no-duplicates)
8. [Out of scope and open questions](#8-out-of-scope-and-open-questions)

## Summary and recommendation

**Recommendation: go on Stage 0 and Stage 1 (small, independently useful, no
new engine); hold Stage 2 (a row builder) behind an explicit evidence gate;
no-go on a general-purpose grid router.**

- The report's premise ("there is no netlist→layout path") is stale. Digest,
  plan validation, plan execution and bundle routing all shipped (section 1).
  The remaining gap is narrower: **heterogeneous** devices sharing diffusion in
  a row, pitch-constrained routing with static obstacles, and ports at
  caller-set coordinates.
- Three reports (#346, #1116, #3922) are real evidence of recurrence, but they
  are **not** three reports of this specific gap. #346 and #1116 were about the
  netlist-to-layout path that has since shipped; only #3922 describes
  shared-diffusion rows with a fixed pitch. Counting it as "three instances" of
  the narrow gap overstates the demand (section 2).
- The cheapest, most defensible work is on the **verification side**: the
  curated gf180mcu deck transcribes only the ordinary metal spacing rule and
  does not list the wide-metal rule as unmodeled, so a curated PASS silently
  omits a rule class the reporter says changed their pitch (section 5). That is
  a coverage-honesty fix that benefits every block, not only this one.
- A row builder is plausibly worth building, but the evidence for it is one
  unreproduced bespoke generator. Stage 2 should be gated on a second consumer
  and on the reporter's generator being shared as a worked example.

## 1. Capability map: what exists, what the gap actually is

Addresses acceptance criterion 1.

### 1.1 What already ships (all **read**)

| Capability | Where | Notes |
| --- | --- | --- |
| SPICE → flat device/net digest | `src/klayout_tools/netlist_digest.py` (Phase A) | Library only, no verb |
| Declarative plan contract + validator | `src/klayout_tools/layout_plan.py`, `docs/cli/layout-plan.md` (Phase B) | `klt.layout_plan.request/1` |
| Plan execution onto `klt gen` + `klt gen-compose` | `src/klayout_tools/layout_plan_execute.py`, `docs/cli/layout-plan-execute.md` (Phase C) | Library only; the doc states "There is no `klt layout-plan execute` verb" |
| N-pin (bundle) routing | `gen_compose.route_bundle` (#1073, Phase D) | Spanning tree of two-pin legs, each through `route_two_pin`'s checks |
| Channel track assignment | `docs/design/gen-compose-track-assignment.md` (#1467) | Reactive per-channel y-offset retry on one layer |
| Waypoint / A\* steering for a leg | `docs/cli/gen-compose.md` | Bounded; "not a general obstacle-avoiding router" |
| Homogeneous shared diffusion and mirroring | `mos_array` params `share_sd` (#3551) and `mirror` (#3552), `docs/cli/gen.md` | Added after #346/#1116; **uniform** unit devices, `fingers: 1`, `topology: "array"` only |
| Dummy devices with optional ports | `mos_array` `dummy`, `dummy_ports` (#2904) | |
| IO pin edge/position constraints | `io.pin_constraints` + `pins[]` report (#3885 → PR #3887, merged 2026-10-18) | **Digital** `klt place-and-route` (OpenROAD) only |
| Split-supply diagnostic | `klt extract` `split_net_labels[]` (#3920 → PR #3926, merged 2026-10-18) | Extraction side; see 1.3 |

Phases A–D are delivered and are not reopened here. Phase E (a worked canary)
was never done for a block of this kind.

### 1.2 The narrower gap

The reporter's block (**reported**) mixed inverters, transmission gates, a NOR3,
a latch-type sense amplifier and flops (~100 transistors) at a fixed 17.84 µm
horizontal pitch. What that needs and the current path does not do:

| Need | Why composition of named generators does not cover it | Class |
| --- | --- | --- |
| **G1. Shared-diffusion chains of *different* devices** with S/D orientation derived per device so facing terminals share a net, hard error on mismatch | `mos_array share_sd` abuts *identical* units in a regular grid; each `klt gen` call emits one cell, and `gen-compose` places whole cells, so there is no unit of "device k in a chain" with a derivable orientation | Engine capability |
| **G2. N/P strips between shared rails**, one poly stripe for a common n/p gate, shared rails, tie strips and well sizing | No generator draws a two-polarity strip; `diff_pair` is a fixed common-centroid cross-quad | Engine capability |
| **G3. Fixed-pitch grid routing with static obstacles** whose pitch makes "different nets on different nodes" legal by construction | `gen-compose` routing is a fixed one-jog backbone plus bounded retry/A\*; it rejects or reports obstacle conflicts rather than planning on a rules-derived grid | Engine capability |
| **G4. Ports at caller-set x** (off-grid), joined to the grid by a jog | `gen-compose` takes port coordinates from the block's own report; there is no "pin at x" request on the compose path. #3885 solved the analogous problem for the OpenROAD digital flow only | Mostly contract/ergonomics |
| **G5. Deterministic byte-identical output** | Already a stated guarantee of the existing generators | Not a gap |
| **G6. A shell verb for plan execution** | Library only today | **CLI ergonomics**, not engine |

**Distinguishing ergonomics from engine.** G6 and most of G4 are ergonomics: the
logic exists or is a small extension, and the missing piece is a verb or a
request field. G1–G3 are engine capability: no existing module draws or routes
that geometry, and they are the bulk of the reporter's ~1,100 lines.

### 1.3 Closure is not capability

The issue directs that #3885 and #3920 closing must not be read as general
physical capability. Reading what they delivered:

- #3885 constrains pins for **OpenROAD standard-cell** blocks. It does not give
  a custom (`gen`/`gen-compose`) block a port-at-coordinate. A custom row builder
  cannot use it. (**read**)
- #3920 adds a `split_net_labels` warning to `klt extract`. The LVS-side finding
  `net.supply_fragmented` (#2078) still fires only on the inline form per the
  issue text (**reported** in #3920; the extract field itself is **read** in
  `docs/cli/extract.md`). Whether the pre-extracted LVS form now raises it was
  not checked here.

## 2. Recurring need and product value

Addresses acceptance criterion 2.

### 2.1 Evidence, with dates

| Report | Date | What it actually described | Still the same gap? |
| --- | --- | --- | --- |
| #346 | 2026-08-03 | gf180-bandgap hand-wrote `generate.py` + `netlist_model.py` + `plan.py`; no netlist-to-layout path | **No.** That path shipped 2026-08-17/18 |
| #1116 | 2026-08-17 | Second block: single/few-transistor stages, resistor strings, ≥3-pin nets | **Mostly no.** Bundle routing (#1073) and plan execution (#1155) shipped the next day |
| #3922 | 2026-10-18 | Third block: ~100 mixed digital transistors, fixed pitch, shared diffusion, off-grid ports | **Yes** — this is the narrow gap |

So the honest count is **one** report of the narrow gap, plus two earlier
reports whose underlying path was delivered. The "3 reports" framing in the
historical alternatives block overstates recurrence for the row builder
specifically. It is, however, fair evidence that **bespoke `klayout.db`
generators are a recurring pattern in downstream repos** — each report ends with
a hand-written generator — which is the product-level signal.

### 2.2 Consumers and what a shared surface would remove

- **Known consumers (reported):** three downstream chip repos, all external to
  this checkout. Their design details stay in those repos per the friction
  protocol. Their number, frequency and which would adopt a shared verb were
  **not** established here; that would need a question to each owner.
- **What would be removed:** for the third block, the reporter's own list is
  six items, roughly 1,100 lines. A reusable surface would remove the *generic*
  portion (flattening, orientation derivation, rail/strip drawing, rules-derived
  pitch, obstacle handling) and leave the block-specific device order and
  external-port coordinates in the caller. How much of the 1,100 lines is
  generic is **unknown** until the generator is read; the reporter offered to
  share it.
- **Alternatives to a shared engine:** (a) document the bespoke pattern: "draw
  with `klayout.db`, then run the same `klt drc/lvs/extract` flow" — the
  checking half already works per the reporter; (b) ship the reporter's
  generator as an `examples/` worked example; (c) extend `mos_array`/`gen-compose`
  incrementally; (d) build a row builder.

### 2.3 Value and maintenance burden

- **Value of (d):** it would close the closed-loop goal for mixed-logic custom
  blocks, which the ROADMAP's non-goals only partially exclude ("block-level
  placement and routing aids are fair game when an epic spikes them"). It does
  not conflict with the full-chip P&R non-goal if scoped to one row-based block.
- **Maintenance burden (estimate, not measured):** a PDK-parametric row builder
  carries per-PDK rule tables (poly/diff/well enclosure, contact pitch, tie
  strips) that must track every supported family. The repo already pays this
  cost for `mos_array`, but a two-polarity strip multiplies the rule surface
  (well, implants, taps). Each new family is a recurring cost.
- **Why now vs. defer:** there is one narrow-gap report and an unread
  reference generator. The defensible answer is "invest in the cheap, shared,
  evidence-independent pieces now; defer the engine until a second consumer or
  the shared generator justifies its shape" (section 6).

## 3. Survey: wrap, extend, or build

Addresses acceptance criterion 3.

**Evidence limits.** The external-engine facts below are **prior-spike**: they
were fetched live on 2026-07-31 by
[`layout-generator-spike.md`](layout-generator-spike.md) and
[`gen-composition-spike.md`](gen-composition-spike.md) and were **not
re-fetched** for this study. Licenses and maintenance status can change; any
decision that hinges on one needs a fresh check first. Nothing external was
executed.

### 3.1 External candidates

| Candidate | License (prior-spike) | What it offers for G1–G4 | Integration constraint | Verdict for this gap |
| --- | --- | --- | --- | --- |
| BAG3 + xbase | BSD-3 AND Apache-2.0 (core); xbase Apache-2.0; `bag3_analog`/`bag3_digital` no LICENSE | Template + routing-grid + tech-plugin abstractions; row/track idioms | Multi-repo C++/Python stack (`pybag`/`cbag`); second geometry backend; no public sky130 primitives library | Ideas only (track-grid abstraction informs G3) |
| laygo2 | BSD-3 | Gridded-object placement closest in spirit to row placement of unlike cells | `gdspy` backend; "Maintenance Mode" since 2024-09 | Ideas only; the `Grid` factoring is the useful part |
| gdsfactory + `gdsfactory/skywater130` | MIT | MIT sky130 device generators with design-rule constants; named ports | `gdstk` backend, a second geometry engine | Rule-constant source and differential-test oracle, not a dependency |
| ALIGN | BSD-3 | Constraint-driven placement/routing of analog blocks from netlists; JSON design-rule abstraction | C++ toolchain; targets its own mock FinFET PDK | Idea source; heavy to wrap, PDK mismatch |
| MAGICAL | BSD-3 | Optimization-based analog placement | Stale since 2024-04; heavy native deps | Not viable |
| KLayout PCells / `pya` | GPL-3.0 (already the repo's anchor dependency) | In-engine geometry, the backend every `klt` verb already uses | None new | **The backend for any build** |

None of these candidates targets *transistor-level custom-digital rows on
gf180mcu/sky130 with a fixed pitch*; the nearest (laygo2, BAG xbase) are
gridded-template frameworks that a user programs per cell, i.e. the same
bespoke-generator pattern the reporter already followed. Whether any of them
could express the reporter's block without the same amount of code was **not
evaluated**.

### 3.2 Internal modules

| Module | Reuse for | Gap |
| --- | --- | --- |
| `layout_plan.py` / `layout_plan_execute.py` | Contract shape, validation, parameter resolution, response envelope | Topologies are generator-keyed; no "row of unlike devices" topology |
| `gen_compose.py` + `gen_compose_routing.py` + `gen_compose_port_access.py` | Port model, obstacle checks, bundle routing, track assignment | One-jog backbone + bounded A\*; no rules-derived grid |
| `gen_pcells/` (`mos_array` etc.) | Unit device drawing, per-PDK rule tables, `share_sd`/`mirror` | Uniform units only |
| `netlist_digest.py` | Flattened device list with W/L | None for this gap |
| `drc.py`, `decks/`, `lvs.py`, `extract.py` | The whole verification half | Rule coverage (section 5) |

### 3.3 Comparison and gates

| Option | What it is | Gate check |
| --- | --- | --- |
| **Wrap** an external engine | Drive ALIGN/BAG/laygo2 from `klt` | Fails "wrap the proven engine": none is proven on these PDKs and each brings a second backend or a heavy native stack. No candidate clears it today |
| **Extend** current code | Heterogeneous `share_sd`-style chains in a new generator family; `gen-compose` port-at-x; rules-derived pitch helper | Consistent with ARCHITECTURE (contract-first, one backend). Lowest risk |
| **Build** a new row generator + grid router | New module family and routing engine | The **rewrite rule** (bottleneck, oracle, unlock) applies to replacing a wrapped engine; this is a new capability, so the "spiked epic" gate applies. Not satisfied until the contract and oracle below exist and a second consumer appears |

ROADMAP scope check: the Non-goals exclude **full-chip** P&R and allow
block-level aids when spiked. A single-row custom block generator is inside that
boundary; a general placer/router is not, and this study does not propose one.

**Recommendation from the survey:** extend, using `pya` and the existing plan
contract, and treat gdsfactory's sky130 rule constants only as a differential
oracle. Do not build a general router.

## 4. Representative worked example

Addresses acceptance criterion 4.

**Provenance.** Source is issue #3922's "Original report" (filed 2026-10-18).
Every number below is **reported** and unverified. The block's schematic,
netlist and generator stay in the owning chip repository.

### 4.1 Generic example derived from the report

A *generic* block exposing the same constraint classes without any
design-specific content. Proposed name: `row_mix_gf180` (gf180mcu, ~20 devices,
sized to be reviewable by hand):

- Two rows (one N strip, one P strip) between a shared VSS and VDD rail with tie
  strips.
- Devices: 2 inverters, 1 transmission gate, 1 NOR3, plus 2 dummy gates —
  enough to need an inverter→TG→NOR3 diffusion chain with orientation
  derivation.
- A **fixed horizontal pitch** (set smaller than the isolated-device layout so
  sharing is required to fit).
- Three terminals required at **caller-specified x**, one off the routing grid.
- A control net that **continues across the abutment** to a neighbour copy.
- Two same-net supply rails that need an explicit strap (the failure mode in
  #3920).

A hand-authored instance of this example (not derived from the reporter's
generator) lives in [`examples/row-mix-gf180/`](../../examples/row-mix-gf180/);
its README records what was executed and what was not.

### 4.2 What it must exercise

| Concern | Specification |
| --- | --- |
| Determinism | Two runs produce byte-identical GDS (compare sha256) |
| Device/connectivity equivalence | Extracted device list equals the netlist's flattened devices, including per-device W/L within one dbu; LVS `match` |
| Shared diffusion | Facing S/D diffusions of chain neighbours are one shape; orientation derived, so a net mismatch between facing terminals is a hard error |
| Supply continuity | One extracted net per supply name; no `split_net_labels[]` entry |
| Pitch | Cell outline width equals the requested pitch; mirrored copy abuts with shared rails |
| Obstacles | Static geometry (contact bars, rails, port stubs) is avoided with computed clearance |
| Ports | Each declared port's pin lies at the requested x |

### 4.3 Meaningful failure cases (the oracle must reject these)

1. Chain order that puts two different nets on facing diffusions → hard error
   naming the pair.
2. Pitch smaller than the minimum legal width for the chain → error with the
   deficit in dbu, not a DRC-dirty layout.
3. Two ports requested closer than the metal spacing → error naming both.
4. Netlist device with no row assignment → error, not silent drop.
5. A wide metal edge (≥ 10 µm in the reported gf180mcu case) placed at the
   curated-deck spacing → must be caught by the native deck, and the study
   expects the curated deck to *miss* it (see 5.2). This case is the one that
   shows whether the verification gap is real.

The reporter's generator is a **candidate example, not proof of general engine
capability**: it demonstrates feasibility for one block, not that its approach
generalises.

## 5. Proposed contract and verification oracle

Addresses acceptance criterion 5.

### 5.1 Request/response contract (proposal)

Prefer an **extension of `klt.layout_plan.request/1`** over a new schema, so
Phases A–C remain the single entry. Additive, versioned fields only.

```json
{
  "schema_version": "klt.layout_plan.request/1",
  "device_groups": [
    {"id": "chain_n", "generator": "mos_row", "devices": ["MN1", "MN2", "MN3"],
     "order": ["MN1", "MN2", "MN3"], "params": {}}
  ],
  "rows": [{"id": "n_strip", "kind": "diffusion_strip", "polarity": "n",
            "groups": ["chain_n"], "rails": {"bottom": "VSS"}}],
  "grid": {"pitch_um": 17.84, "layer_role": "metal", "obstacle_clearance": "from_rules"},
  "ports": [{"name": "A", "x_um": 3.17, "layer_role": "metal"}]
}
```

- `mos_row` is a **proposed** generator name, not a commitment. Whether it is a
  `klt gen` family or a plan-level construct is an open question (section 8).
- Response: the existing plan-execute envelope plus `chain[]` (per device:
  resolved orientation, left/right net), `grid` (pitch, tracks used, clearance
  per obstacle) and `ports[]` (requested vs achieved x).
- Failure uses the shared envelope and exit codes
  (`docs/json-contract.md`); each failure case in 4.3 is a distinct
  `error.code`.

### 5.2 Independent verification oracle

"Independent" means not computed by the generator itself:

1. **Connectivity and devices:** `klt extract` then `klt lvs` against the source
   netlist; device W/L compared per device.
2. **DRC, both engines:** `klt drc --deck <pdk>` (curated) **and**
   `klt drc --engine klayout` (PDK-native). Disagreement between the two is
   itself a finding.
3. **Determinism:** double-run hash equality.
4. **Diagnostics:** every 4.3 failure produces its named error.
5. **Optional second implementation:** gdsfactory's MIT sky130 rule tables as a
   differential oracle for rule constants (prior-spike recommendation).

### 5.3 What current checks establish, and what they do not

- **Established (read):** the curated gf180mcu deck transcribes
  `metal1`–`metal5` width and space as single thresholds, e.g.
  `metal2.space.1` = 0.28 µm from DRM rule `Mn.2a`
  (`src/klayout_tools/decks/gf180mcu.py`).
- **Not covered, and not disclosed (read):** the file contains no wide-metal
  spacing id (no `Mn.2b`-style rule; a search for `Mn.2b`/`wide` finds nothing
  relevant), and the deck's end-of-line companions for contact and via
  enclosure are documented as not transcribed
  (`CO.6a`/`CO.6b`, `Vn.3c`/`Vn.3d`, `Vn.4b`/`Vn.4c`). The published
  `unmodeled_rules` list for gf180mcu names `MT30.*`, `MT.2b`/`MT.3`/`MT.4`,
  `MSLOT.*` and the `ESD.*`/`GR.*` remainder — **not** the wide-metal rule for
  `Metal1`–`Metal5`, nor the narrow-line end-of-line rules. That omission means a
  curated PASS reads as fuller coverage than it is, which matches the
  reporter's account. This is **read from source**; I did not run a layout
  through both engines to confirm the reported divergence.
- **Unmeasured:** native-engine coverage (the reporter says 640 rule
  categories), the actual wide-metal threshold for this PDK variant, and
  whether the curated deck has any rule that would fire on the report's
  geometry. A curated-deck PASS alone is not evidence of complete physical-rule
  coverage, and nothing in this study claims otherwise.
- **Tests today:** `tests/test_layout_plan.py`, `tests/test_layout_plan_execute.py`
  and `tests/test_gen_compose*.py` cover the shipped phases on generator-built
  blocks. None builds or checks a mixed-device shared-diffusion row.

## 6. Staged path, estimates, and go/no-go

Addresses acceptance criterion 6. **All estimates are judgement, not
measurement**, expressed in Builder-sized issues (about one PR each).

### 6.1 Stages

| Stage | Content | Est. | Depends on | Independently useful? |
| --- | --- | --- | --- | --- |
| **0a. Coverage honesty** | Add the missing gf180mcu wide-metal and narrow-line end-of-line ids to `unmodeled_rules` so a curated PASS discloses them | 1 issue, small | none | Yes — benefits every gf180mcu block |
| **0b. Worked example** | Obtain the reporter's generator as an `examples/` entry with provenance (Phase E) | 1 issue + owner action | Reporter consent | Yes; also unblocks sizing Stage 2 |
| **0c. Shell verb** | `klt layout-plan execute` over the existing library (G6) | 1 issue | none | Yes; pure ergonomics |
| **1a. Port at coordinate** | `ports[]`-at-x request on the compose path, jog to grid (G4) | 1–2 issues | none | Yes, for any composed block |
| **1b. Rules-derived pitch helper** | Library function: legal pitch from width/space/EOL/wide-metal rules for a layer, reusing `decks/` data (feeds G3) | 1–2 issues | 0a | Yes; also a DRC-hint |
| **1c. Native-deck cross-check** | Mode that runs curated and native decks and reports disagreement (oracle 5.2.2) | 1–2 issues | none | Yes |
| **2. Row builder** | `mos_row`: heterogeneous shared-diffusion chains, N/P strips, rails, ties (G1, G2) | 5–8 issues | 0b, 1a, 1b, a second consumer | Only with its gate |
| **3. Grid router** | Rules-derived grid routing with obstacles (G3) | 5+ issues, high variance | Stage 2 | **No-go** unless Stage 2 shows `gen-compose` cannot suffice |

Recurring cost for Stage 2: per-PDK rule tables for each supported family and a
golden-GDS corpus; estimate roughly one rule-table pass per new family.

### 6.2 Stop/go conditions

- **Go on Stage 0/1** now: each is small, in scope, uses one backend, and does
  not depend on this study's larger question.
- **Go on Stage 2 only if all hold:** (a) the reporter's generator is shared and
  a reviewer judges a majority of it generic rather than block-specific;
  (b) a second consumer with the same shared-diffusion need is identified; (c)
  Stage 1b's pitch helper has been used by that generator without needing a
  router; (d) the 4.3 oracle can be run in CI.
- **Stop / defer** if (a) fails: ship the example and a "draw with `klayout.db`,
  then run `klt drc/lvs/extract`" recipe instead. This is a legitimate outcome,
  not a failure.
- **No-go on a general router** (Stage 3): it strains the block-level scope,
  fails the "wrap the proven engine" gate (no candidate clears it), and the only
  evidence is one block routed on a fixed grid, which a rules-derived pitch
  helper may already handle.

No implementation issue is auto-approved by this study; each stage is a
candidate for normal Curator/Champion evaluation.

## 7. Related work reviewed (no duplicates)

- **#3885 / PR #3887 (merged 2026-10-18):** pin constraints for OpenROAD blocks;
  not usable for custom rows (1.3). Stage 1a is the custom-flow analogue, not a
  duplicate.
- **#3920 / PR #3926 (merged 2026-10-18):** `split_net_labels` in `klt extract`;
  it is the extraction half of the supply-strap pitfall. Whether the pre-extracted
  LVS form should also report `net.supply_fragmented` is a possible follow-up,
  not part of this study.
- **#3299 / #3551 / #3552 (closed):** homogeneous `share_sd`/`mirror`; the
  closest shipped capability and the natural seed for G1.
- **#1467 / #1073 / #1155 (closed):** track assignment, bundle routing and plan
  execution; reused, not reopened.
- **#2078:** `net.supply_fragmented` on the inline LVS form; referenced for the
  diagnostic pitfall.
- An open-issue duplicate search at curation time (2026-10-20) found no related
  open work. The curated gf180mcu deck gap in Stage 0a was **not** searched
  against open issues by this PR; check before filing.

## 8. Out of scope and open questions

**Out of scope (per the operator ruling):** any generator, router, CLI verb or
deck-rule code; any simulation or EDA run; spend, deployment, new host access;
full-chip P&R; the reporter's design-specific content.

**Open questions:**

1. Is `mos_row` a `klt gen` family or a plan-level construct? The former keeps
   geometry in generators; the latter keeps `klt gen` single-cell. Not resolved.
2. Does the repo want the curated deck to model wide-metal and end-of-line rules
   (extending `DrcRule`'s vocabulary, which today cannot express edge-length or
   narrow-line predicates), or only disclose them (Stage 0a)?
3. Which PDK variants does the wide-metal gap actually affect? The report names
   gf180mcu; sky130 and sg13g2 were not checked.
4. How much of the reporter's generator is generic? Unanswerable until it is
   read.
