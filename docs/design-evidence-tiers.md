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

## T1 checklist — what "sim-validated" requires

Every item names the artifact and the pass condition. "Fresh" means the
artifact's provenance (input hashes, netlist/layout revision) matches the
block's current sources — a passing report against last week's netlist is
evidence of nothing.

This list assumes an **analog** block — schematic capture, PVT corner
sweeps, Monte Carlo on statistical rows. A digital or mixed-signal block
does not restate this list; it maps each item through "Digital and
mixed-signal T1 equivalents" below instead.

1. **Schematic** — committed schematic sources (or generator) plus the
   netlist derived from them, regenerated on design change.
2. **Layout** — committed GDS/OASIS, reproducibly generated or with
   documented provenance.
3. **DRC clean** — latest `klt drc` JSON report: `status: clean`, fresh,
   with the deck identified (content hash). Known deck coverage gaps
   (rule-free layers, skipped rules) must be enumerated in the claim, not
   hidden behind "clean" — a clean verdict from a deck with undisclosed
   holes is a false claim.
4. **LVS clean** — latest LVS report `status: match`, fresh, engine named.
   Warnings-only mismatches must be listed with the claim. A second,
   independent engine's concurring verdict (#343) strengthens this from
   "one toolchain agrees with itself" to a cross-checked result.
5. **Full PVT corner simulation vs a ratified spec** — corner-matrix
   results covering every spec row at its bound corners, with per-row
   pass/fail and the binding corner recorded. Requires the spec table
   itself to be ratified — verdicts against a draft spec are provisional
   by construction.
6. **Statistical claims carry Monte Carlo evidence** — any accuracy,
   offset, or matching spec row is statistical; a corner matrix cannot
   validate it. MC runs need a recorded seed, sample count, a
   deterministic negative control, and results combined with (not instead
   of) process corners (#344).
7. **Post-layout simulation** — the spec suite re-run against the netlist
   extracted from the layout, not only the drawn schematic (#252). Until
   parasitic extraction lands (#217), state what the extracted netlist
   does and does not model.
8. **Characterization report** — one aggregated, current artifact
   summarizing per-spec-row performance across conditions, with the
   evidence record each verdict rests on (#309 tracks the aggregation
   tool).
9. **Testbenches shipped** — every claimed measurement's testbench
   committed, with a documented cold-start invocation a third party can
   run; pinned PDK revision.
10. **Repo hygiene** — a README stating what the block is, its spec table,
    and how to reproduce every result; a license; CI that at minimum
    keeps the harness and evidence formats valid.

## Digital and mixed-signal T1 equivalents

The checklist above was written for a block with a schematic. A block whose
design entry is RTL — or a mixed-signal block with an RTL-entry partition —
does not have a schematic to capture, a PVT corner *sim* to run, or (usually)
a statistical spec row. Each item below still names an artifact and a pass
condition; a block just states which reading it is grading against
(`analog` or `digital`) so a reviewer knows which artifact to expect.

1. **Schematic → RTL + gate-level netlist** — committed RTL sources plus the
   synthesized gate-level netlist derived from them (`klt synthesize`),
   regenerated on design change. Same freshness rule as the analog item:
   a netlist that predates the RTL it should have been regenerated from is
   not evidence.
2. **Layout → routed GDS from P&R** — committed GDS/OASIS produced by
   place-and-route (`klt place-and-route`) from the item-1 netlist,
   reproducibly generated (the P&R run is scripted/reproducible, not a
   one-off hand edit). **Capability gap**: `klt place-and-route` is
   sky130hd-only today; no gf180mcu digital P&R path exists yet (#637). A
   gf180mcu digital or mixed-signal-digital block cannot produce this
   artifact and must say so as "blocked on #637" — not mark the item
   ABSENT with no forward pointer, and not silently drop it from the claim.
3. **DRC clean — unchanged.** Applies as written to the routed GDS from
   item 2. On a PDK/flow combination without a P&R path (the #637 gap),
   this item is blocked by item 2's absent input, not by anything specific
   to DRC — say "blocked on #637 via item 2," not "DRC not attempted."
4. **LVS clean — unchanged**, checked against the gate-level netlist from
   item 1 rather than a schematic netlist. Same #637 blocker as item 3
   where no routed GDS exists yet.
5. **PVT corner sim vs a ratified spec → multi-corner STA + bit-exact
   functional suite** — static timing analysis (setup and hold) across the
   PVT corner set, plus the functional test suite (`klt
   functional-verification`) run bit-exact against a ratified spec, with
   per-corner and per-test pass/fail recorded. Same ratification
   requirement as the analog item: verdicts against a draft spec are
   provisional by construction.
6. **Monte Carlo — applies only where a spec row is statistical.** Most
   digital spec rows (functional correctness, Fmax, timing closure) are
   not statistical, and a block whose spec has no statistical row must
   state that explicitly in its claim rather than silently omit the item —
   an omitted item reads as an oversight, a stated N/A reads as a scoped
   claim. Where a spec row genuinely is statistical (a mixed-signal
   block's analog partition, or a digital block claiming a statistical
   yield/timing-margin result), it still needs the analog item's full MC
   evidence — seed, sample count, deterministic negative control, combined
   with process corners (#344) — a timing-corner sweep is not a substitute.
7. **Post-layout sim → functional suite re-run against the post-route
   netlist with SDF timing** — the same functional suite from item 5,
   re-run against the post-route gate-level netlist (not the pre-route
   synthesis netlist) with back-annotated SDF timing. Same #637 blocker as
   items 3–4 where no routed netlist exists yet.
8. **Characterization report → Fmax / area / power across corners** — one
   aggregated, current artifact reporting Fmax, area, and power across the
   corner set, with the evidence record each figure rests on (#309 tracks
   the aggregation tool, same as the analog item).
9. **Testbenches shipped — unchanged.** Every claimed measurement's
   testbench (cocotb or equivalent) committed, with a documented
   cold-start invocation a third party can run; pinned PDK **and**
   toolchain revision (Yosys/OpenSTA/etc., same discipline as pinning
   ngspice for the analog item).
10. **Repo hygiene — unchanged.**

### Mixed-signal blocks: both checklists, scoped per partition

A mixed-signal block does not average the two checklists — it partitions
into an analog sub-block and a digital sub-block and satisfies **both**
checklists in full, one per partition. The claim must name the boundary
explicitly (which nets/pins/cells sit on which side, e.g. "digital control
FSM and register file: items 1–10 digital; analog front-end from `vin` to
the ADC output codes: items 1–10 analog") — an unstated boundary leaves a
reviewer unable to tell which evidence covers which silicon, which is the
same coverage-honesty failure the "Verification rules" below call out for a
single checklist. Item 6 (Monte Carlo) is evaluated per partition against
that partition's own spec rows, not block-wide — a digital partition with
no statistical row states N/A even if the analog partition alongside it
carries full MC evidence.

Until a capability gap like #637 closes, a mixed-signal block's digital
partition inherits that gap's blocker (see item 2 above) independently of
its analog partition's status — a clean analog-partition claim does not
paper over a blocked digital-partition item, and the reverse.

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
checklist into a per-repo qualification report. It does not yet select the
analog or digital/mixed-signal checklist automatically (#640) — until it
does, state the block kind and map by hand per the section above.
