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

### Block kind

A T1 claim states the block's **kind**: `analog`, `digital`, or
`mixed-signal`. The kind determines which column of items 1, 2, 5, 6, and 7
applies — items 3, 4, 9, and 10 are kind-independent and apply as written to
every block.

- **Analog** blocks satisfy the *Analog* column only.
- **Digital** blocks satisfy the *Digital* column only. Nothing in the
  Analog column (schematic capture, PVT corner sweeps, Monte Carlo) is
  applicable or required — a digital block is not held to analog artifacts
  it has no reason to produce.
- **Mixed-signal** blocks partition into analog and digital sub-blocks
  within the same repo and satisfy **both** columns, one per partition. The
  claim must state the partition boundary explicitly (which nets/pins/cells
  belong to which side) so a reviewer can tell which evidence covers which
  silicon.

1. **Design sources**
   - *Analog* — committed schematic sources (or generator) plus the
     netlist derived from them, regenerated on design change.
   - *Digital* — committed RTL sources plus the synthesized gate-level
     netlist derived from them, regenerated on design change.
2. **Layout**
   - *Analog* — committed GDS/OASIS, reproducibly generated or with
     documented provenance.
   - *Digital* — committed routed GDS/OASIS from place-and-route,
     reproducibly generated from the gate-level netlist (P&R script/flow
     committed, not a one-off hand edit).
3. **DRC clean** — latest `klt drc` JSON report: `status: clean`, fresh,
   with the deck identified (content hash). Known deck coverage gaps
   (rule-free layers, skipped rules) must be enumerated in the claim, not
   hidden behind "clean" — a clean verdict from a deck with undisclosed
   holes is a false claim.
4. **LVS clean** — latest LVS report `status: match`, fresh, engine named,
   checked against the netlist from item 1 (schematic netlist for analog,
   synthesized/routed gate-level netlist for digital). Warnings-only
   mismatches must be listed with the claim. A second, independent engine's
   concurring verdict (#343) strengthens this from "one toolchain agrees
   with itself" to a cross-checked result.
5. **Full corner verification vs a ratified spec**
   - *Analog* — PVT corner-matrix simulation results covering every spec
     row at its bound corners, with per-row pass/fail and the binding
     corner recorded.
   - *Digital* — multi-corner static timing analysis (setup and hold
     across the PVT corner set) plus a bit-exact functional test suite,
     with per-corner and per-test pass/fail recorded.
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
     pre-layout RTL/gate simulation.
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

## Verification rules

- **Staleness is failure.** Every report is checked against current source
  revisions/hashes before it counts. Provenance blocks in klt JSON output
  (#335) exist for this.
- **Coverage honesty.** A verdict's blind spots (deck holes, warning-level
  mismatches, uncombined evidence legs) are part of the claim and travel
  with it.
- **No claim without a testbench.** A spec row with no runnable bench
  checking it is unaddressed, whatever the prose says.
- **Downgrades are automatic.** Evidence going stale (design change without
  re-run) drops the block below the tier until re-established.

The `design-signoff` skill (`.claude/skills/design-signoff/`) turns this
checklist into a per-repo qualification report.
