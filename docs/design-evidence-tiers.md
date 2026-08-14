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
     the layout, not only the drawn schematic (#252). Until parasitic
     extraction lands (#217), state what the extracted netlist does and
     does not model. A `klt pex` JSON report — the per-corner,
     per-spec-row schematic-vs-extracted delta — is the machine-checkable
     evidence for this item, and the *only* evidence `klt signoff`'s
     tier-verdict mode accepts for it: unlike every other item, item 7
     rejects a passing citation of any other kind, since a clean DRC or a
     pre-layout schematic sim proves nothing about post-layout behaviour
     (#871). `klt pex` (Epic #709, issue #801) is implemented — see
     `docs/cli/pex.md` for its full contract and `docs/cli/signoff.md`'s
     "Item 7 is kind-restricted" section for how `klt signoff` binds to it.
   - *Digital* — the functional test suite re-run against the post-route
     gate-level netlist with back-annotated SDF timing, not only the
     pre-layout RTL/gate simulation.
8. **Characterization report** — one aggregated, current artifact
   summarizing per-spec-row performance across conditions, with the
   evidence record each verdict rests on (#309 tracks the aggregation
   tool). For a digital or mixed-signal digital partition this includes
   Fmax, area, and power across the corner set, not just functional
   pass/fail.
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
