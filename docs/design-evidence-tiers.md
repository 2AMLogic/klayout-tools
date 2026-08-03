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
