# Design note: fleet-scheduler unit abstraction for digital work (candidate vs. corner)

**Status:** design note / decision record. Nothing here authorises
implementation, and no `klt` code is touched by this document. Per
[docs/ARCHITECTURE.md](../ARCHITECTURE.md) → "How capabilities arrive," this
is Phase 1 of
[Epic #391](https://github.com/2AMLogic/klayout-tools/issues/391) (adopt the
digital engine class — Yosys + OpenROAD), resolving the specific question
[issue #400](https://github.com/2AMLogic/klayout-tools/issues/400) files:
does [Epic #375](https://github.com/2AMLogic/klayout-tools/issues/375)'s
fleet scheduler (`remote.hosts`, AWS, spend-permissioned, shard/merge,
fleet-level cost gates) generalize to digital design-space exploration
before Epic #391's own Phase 6 (Elastic) assumes it does.

## Why this needs answering before Phase 6

Epic #391's own body names the risk directly: "SPICE parallelism is corners
× MC — many pure, re-runnable, independent units. A single synth+P&R run is
not that; it is one long serial job. Digital work becomes embarrassingly
parallel only when the unit is 'one candidate evaluation' in a
design-space exploration, not 'one corner.' If the fleet scheduler is to
generalize, its unit abstraction has to be the candidate, not the corner."
This note grounds that claim in what #375's scheduler actually is (not what
it might be), then states the decision.

## What #375's scheduler actually is (verified against the code)

Two distinct layers exist today, and they generalize differently.

### Layer 1 — the K-host provisioning primitive (`src/klayout_tools/remote_fleet.py`)

`FleetLauncher`/`run_fleet` (Epic #375 Phase 1B, issue #377) provisions `K`
identical EC2 instances, runs a caller-supplied callable against each
concurrently, retries a failed shard once on a fresh instance, and
guarantees teardown of every launched instance on every exit path (success,
exception, or signal). Its own module docstring is explicit about scope:
"Nothing in this module knows what a 'shard' or a 'unit' is beyond an
integer sizing count" — the `ShardRunner` contract is
`(shard_index, launcher, public_ip) -> Any`, and the result is opaque to
`FleetLauncher`. `shard_unit_counts: Sequence[int]` is used for exactly one
thing: sizing the fleet's instance type (`select_instance_type(max(counts),
threads_per_corner)`, `remote_fleet.py:415-417`) and computing the
fleet-level cost gate / vCPU quota check (`require_fleet_cost_gate`,
`check_vcpu_quota`).

This layer is generic over "what a unit is." It does not assume units are
short, does not assume a host runs more than one unit, and does not inspect
the `ShardRunner`'s return value.

### Layer 2 — the corners×MC shard/merge engine (`src/klayout_tools/sim.py`)

`_shard_corner_points`/`_run_sharded` (Epic #375 Phase 1A, issue #376,
documented in `docs/cli/sim.md` → "Fleet sharding (`remote.hosts`)") is
SPICE-specific in three load-bearing ways:

1. **A shard is a *sub-list* of many small units, not one unit.** The
   expanded corner×MC list is sliced into `hosts` *contiguous slices* of
   `len(units) // hosts` units each (`sim.py:1420-1450`) — each host is
   expected to run several units, not one, and runs them through the
   *local* worker pool (`local-parallel`'s existing code, reused unchanged
   per Epic #253/#375's "same-code-different-box" decision) one level
   below the fleet split.
2. **Units are cheap and individually retryable.** SPICE corners are
   independent, pure, ~140 s each (the measured warm-AMI overhead constant
   `T_o ≈ 140 s` that Epic #375's own body cites comes from amortizing
   *spin-up* against *many such units per host* — batching many units per
   host is the entire point, because a single unit's runtime is comparable
   to spin-up time).
3. **The merge target is a flat, homogeneous array.** The report contract
   merges `K` shards' `corners[]` sub-arrays back into one `corners[]` array
   in original order (`docs/cli/sim.md` → "Deterministic merge, any
   completion order") — every unit produces the *same shape* of result
   (pass/fail/error + measurements), and "the merged result" means
   "concatenate."

None of these three properties holds for a digital candidate evaluation
(see below), so Layer 2 does not generalize as written.

## What varies across "candidate evaluations" for this epic's flow

A "candidate" is one complete point in a design-space exploration run
through the full synthesis → verification → place-and-route pipeline (Epic
#391 Phases 2–4), evaluated against the single scored objective #387's gate
consumes. Concretely, for the marketing#56 canary (GCD/RSA modexp on
sky130) and similar blocks, the axes a realistic sweep varies are:

- **Synthesis strategy variants** — different Yosys optimization
  scripts/`abc` passes, different area/timing trade-off flags, different
  target `sky130_fd_sc_hd` cell-drive-strength preferences. Each strategy
  produces a different netlist (different cell count / area / static
  timing) from the *same* RTL + constraints.
- **P&R seed/placement variants** — different global-placement random
  seeds, different floorplan aspect ratios or utilization targets, feeding
  the *same* netlist into OpenROAD and getting different
  wirelength/slack/utilization/DRC-cleanliness outcomes.
- (Later, per Epic #391 Phase 4's floorplan plumbing) **floorplan
  variants** — die/core area, IO strategy — a third axis once P&R is
  wired up.

Each candidate is a full pipeline run: Yosys synthesis, then (once wired,
Epic #391 Phase 5) OpenROAD P&R, then cocotb/Verilator functional
verification as the hard pass/fail gate. This is a *serial, multi-stage,
long-running* job per candidate — not a single short numeric solve.

## Cost/time implications: why the existing model does not carry over

Epic #375's `T_o ≈ 140 s` overhead constant and its Phase 2 "computed K"
model (K derived from unit count `N`, per-host batch size `m`, overhead
`T_o`, and a per-unit runtime estimate `t`) are built around `t` being on
the same order as `T_o` — a single SPICE corner is itself on the order of
seconds to a couple of minutes, so spin-up amortization *is* the dominant
design pressure, and batching many units per host is what makes the
"wall-clock is free, cost is flops-invariant" argument work.

A single synth+P&R+verify run for even a small block is minutes to hours
(OpenROAD placement/routing alone routinely dominates), i.e. `t >> T_o` by
one to several orders of magnitude once real numbers exist (Epic #391's own
OpenROAD survey, issue #397, is where a first measured `t` will come from —
this note does not invent one, for the same reason
`docs/design/lvs-extraction-spike.md`'s scoring section gives: "scoring...
before anything exists... would be inventing a number"). Two consequences
follow directly from `t >> T_o`:

- **Spin-up amortization is not the design pressure for digital.** Whereas
  SPICE benefits from packing many corners onto one host to amortize `T_o`,
  a digital candidate's own runtime already dwarfs spin-up — there is
  nothing to amortize by batching multiple candidates onto one host, and
  doing so would only reintroduce the *intra-host contention* problem the
  single-host `remote` backend was built to avoid (`docs/design/remote-sim-backend-spike.md`
  → decision 2, "one instance per corner *matrix*, not one instance per
  corner," inverted here: one instance per *candidate*, not one instance
  per multiple candidates).
- **Retry economics change.** A lost SPICE shard's retry costs ~140 s plus
  its unit batch's runtime; a lost digital candidate's retry costs a full
  multi-stage pipeline re-run, potentially hours. The one-automatic-retry
  policy (#375 decision 2) is still the right shape (units — here,
  candidates — are pure and re-runnable, and one lost candidate should
  never abort the sibling candidates), but the *K-selection* model that
  weighs "is this retry worth it" needs its own calibration once #397 and
  Epic #391 Phases 2–4 produce a measured `t`, not a reuse of #374/#375's
  formula with SPICE's `T_o`/`t` plugged in.

## Decision

**The unit of parallelism for digital work under Epic #391 is one candidate
evaluation** — one full synthesis→verification→P&R pipeline run for one
point in a design-space sweep (synthesis strategy variant, P&R seed/
placement variant, or later floorplan variant), scored by #387's single
objective. This is the epic body's own framing, confirmed rather than
revised by this note.

**#375's two layers generalize differently, and only one needs no rewrite:**

- **Layer 1 (`remote_fleet.py`'s `FleetLauncher`/`run_fleet`) — re-parameterizes
  in place, no rewrite.** It already treats "what a unit is" as opaque
  (per its own docstring). Digital fleet dispatch can call it with
  `shard_unit_counts=[1] * K` (one candidate per host — no intra-host
  batching, unlike SPICE's per-host batching) and a digital-specific
  `ShardRunner` that runs one candidate's full pipeline and returns its
  scored result. Provisioning, the fleet-level cost gate
  (`require_fleet_cost_gate`), the vCPU quota pre-check
  (`check_vcpu_quota`), the one-retry-per-shard policy, and
  guaranteed-teardown-on-every-exit-path all carry over unchanged — none of
  that machinery is corner-shaped. The one required adjustment is the
  *instance sizing formula* threaded through it: `select_instance_type`'s
  `vcpu_needed = corner_count * threads_per_corner` (`remote_launcher.py`)
  encodes ngspice's own thread-per-process model and is not the right sizing
  input for an OpenROAD P&R job (different, and likely memory- as well as
  CPU-bound, resource shape) — Epic #391 Phase 4's OpenROAD survey (#397)
  is the right place to derive a digital-specific sizing formula to pass
  through the same `select_instance_type`-shaped seam, not a reason to
  change `FleetLauncher` itself.
- **Layer 2 (`sim.py`'s `_shard_corner_points`/`_run_sharded`
  contiguous-slice-and-flat-array-merge engine, and the
  `docs/cli/sim.md` "Fleet sharding" report contract it backs) — does
  *not* generalize as written, and should not be reused for digital.** It
  is built for many-cheap-units-per-host and a homogeneous-array merge
  target; a digital candidate is one long-running unit per host, and the
  meaningful "merge" is not concatenation of a flat list but assembling a
  ranked/scored set of candidate results (feeding #387's scored gate).
  Digital work therefore needs its **own** shard/merge contract, written
  new against the synthesis/P&R/verification JSON contracts this epic's
  Phase 1 is separately proposing (issue #399) — this is genuinely new
  scaffolding, not an extension of `sim.py`'s engine.
- **The K-selection cost/time model (Epic #375 Phase 2, the
  `T_o`-calibrated formula) does not apply to digital and should not be
  reused with substituted constants.** A digital K-selection model needs
  its own measured per-candidate runtime once one exists (post Epic #391
  Phase 2/4), and should be scoped as a distinct Epic #391 Phase 6 issue
  rather than inherited from #375 Phase 2.

**Net**: Phase 6 (Elastic) can reuse `remote_fleet.py`'s provisioning
primitive directly (a re-parameterization, low risk, small surface), but
must budget for **new** shard/merge and K-selection scaffolding scoped to
the candidate-evaluation shape — it is not "generalize #375's scheduler,"
it is "reuse #375's provisioning layer under a new digital-specific
shard/merge layer." Phase 6 issues should cite this note rather than
assume corner-shaped reuse of `sim.py`'s engine.

## What Phase 6 will need to define (not decided here — flagged so the phase issue doesn't skip them)

- A request-shape decision: whether digital fleet dispatch reuses the
  `remote.hosts: K` field name (redefining its unit from "shard of a flat
  list" to "count of candidates, one per host" for digital jobs) or
  introduces a distinct field (e.g. `remote.candidates: K`) to avoid
  overloading a name whose documented semantics (`docs/cli/sim.md`) are
  SPICE-shard-specific today.
- The candidate-result merge/rank contract itself (depends on Phase 1's
  synthesis/P&R/verification contracts, issue #399, and #387's scored-gate
  shape).
- A digital-specific instance-sizing formula for the `FleetLauncher`
  sizing seam (depends on Epic #391 Phase 4's OpenROAD survey, #397, for
  a measured resource profile).
- A digital-specific K-selection cost/time model, once a measured
  per-candidate `t` exists (depends on Phases 2 and 4 landing).

## Out of scope for this note

No dependency was added, no `klt` code was written or modified, and no AWS
resource was touched. This note answers only the unit-abstraction question
Epic #391 Phase 1 (issue #400) requires resolved before Phase 6 begins.
