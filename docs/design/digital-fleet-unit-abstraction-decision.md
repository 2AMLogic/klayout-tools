# Decision: unit of parallelism for digital work under Epic #391

**Status:** decision record, no implementation. This document resolves
Epic #391 ("adopt the digital engine class — Yosys + OpenROAD") Phase 1's
explicit deliverable (issue #400): "resolve what 'one unit' means for
digital work under this epic, before Phase 6 (Elastic) assumes the existing
scheduler generalizes for free." Nothing here authorises implementation, adds
a dependency, touches AWS, or changes `remote_fleet.py` / `remote_launcher.py`
/ `remote_transport.py`. It follows the same wrap/build decision-record
pattern the accepted spikes in this directory use (e.g.
[docs/design/remote-sim-backend-spike.md](remote-sim-backend-spike.md)), adapted
to a scheduler-reuse question rather than an engine or provisioning choice.

## Why this exists

Epic #375 built a fleet scheduler for `klt sim`'s `remote` backend around
SPICE's own parallelism shape: corners × Monte-Carlo samples are many pure,
independent, cheaply re-runnable units (`t ≈ 140 s` each, measured; see
"Grounding" below), packed several-per-host and sharded across `K` hosts.
Epic #391's own body names the risk directly: "a single synth+P&R run is one
long serial job," not that shape, and warns that Phase 6 ("Elastic") must not
"silently assume corner-shaped reuse." This document is that check, written
before any Phase 6 issue exists.

## Grounding: what #375 actually built (read from the merged implementation)

Verified against the merged code, not the epic's proposal text alone:

- **`src/klayout_tools/remote_fleet.py`** (`FleetLauncher`/`run_fleet`,
  merged via #383/#384): a shard is identified only by an integer,
  `shard_unit_counts: Sequence[int]` — used **only for instance sizing**
  (`select_instance_type(max(shard_unit_counts), threads_per_corner)`,
  imported from `remote_launcher.py`). The module's own docstring is explicit
  that it owns *nothing* about what a shard computes: "Nothing in this module
  knows what a 'shard' or a 'unit' is beyond an integer sizing count." The
  actual per-shard work is a caller-supplied callable,
  `ShardRunner = Callable[[int, RemoteLauncher, str], Any]` — opaque to the
  launcher.
- **`src/klayout_tools/remote_transport.py`** (`JobDescription`/`JobInput`,
  merged via #278, documented in
  [docs/design/remote-job-description.md](remote-job-description.md)):
  already generalized to `job.command: str`, `job.inputs: tuple[JobInput, ...]`,
  `job.success_exit_codes`, `job.artifacts_relative_dir` — all data, not
  `klt sim`-specific. That document's own "what a future `extract`/`lvs`/DRC
  remote backend still has to bring" section already anticipates exactly this
  kind of second adopter: its own `JobDescription` builder, its own AMI, its
  own sizing call — none of which requires a change to `remote_transport.py`
  itself.
- **Fleet-level cost gate** (`require_fleet_cost_gate`) and **vCPU quota
  pre-check** (`check_vcpu_quota`): both operate purely on
  `hosts × instance_type`'s rate/vCPU count. Neither knows or cares what
  runs inside a shard.
- **One-retry-per-shard** (`FleetLauncher._run_shard_with_retry`) and
  **teardown-all** (`FleetLauncher.terminate_all`, the context-manager
  guarantee): both are shard-content-agnostic — "a shard's run raised" and
  "tear down every launched instance" have no dependency on the unit shape.
- **Merge/report assembly is explicitly *not* inside `remote_fleet.py`.**
  The module's own docstring assigns "the actual corner/unit slicing, the
  per-shard request document, and the merged-report assembly" to the
  *caller* layer (Phase 1A / `klt sim`'s own `sim.py`), calling this out as
  "the integration point for Phase 1A" — i.e., the one piece of #375 that is
  inherently unit-shaped (corner ordering, per-corner pass/fail merge) was
  designed to live beside the scheduler, not inside it.
- **The scheduling model** (from #374, the architecture issue #375 restructured
  from): `wall(K) ≈ T_o + ceil(N / (K·m)) · t`, with measured constants from
  the #253 live validation (2026-08-03): **`T_o ≈ 140 s`** (per-host overhead:
  provision + boot + SSH-ready + push + pull + teardown, warm-AMI), `t` = one
  ngspice corner's own runtime, `m` = slots per host (`instance vCPU /
  threads_per_corner`, e.g. `c7i.12xlarge` → `m = 6` at 8 threads/corner). This
  model, and its "choose K so per-host compute stays ≥ β·T_o, β ≈ 4–5" rule,
  is what Phase 6 must not assume transfers unmodified — see Question 3 below.

## Question 1: what does "one unit" mean for digital work?

**Decision: the unit of parallelism is "one candidate evaluation"** — one
complete synthesis(+verification)+place-and-route pipeline run, executed to
completion for one point in a design-space exploration — never one pipeline
*stage* (synthesis alone, or P&R alone) and never "one corner" by analogy.

**What varies across candidates, concretely, for this epic's flow** (RTL →
synthesis → [verification] → place-and-route → GDS, sky130 first, per Epic
#391's Phase 2–4 scope):

- **Synthesis strategy variants** — different Yosys `synth` script choices
  (area- vs. speed-optimized `abc` passes, different technology-mapping
  flags, different target clock-period constraints fed to the same RTL) —
  each produces a different netlist (different cell count / area / timing)
  from the identical RTL input.
- **Floorplan variants** — die/core aspect ratio, target utilization, macro
  placement seed — different inputs to the same synthesized netlist going
  into OpenROAD.
- **P&R seed variants** — placement (global placement, detailed placement)
  and routing stages in OpenROAD tools (e.g. RePlAce-style global placement,
  FastRoute/TritonRoute) are seeded; different seeds on the *same*
  netlist+floorplan produce different QoR (timing slack, wirelength,
  congestion) even with no other input change. This is the direct digital
  analogue of Monte-Carlo sampling in SPICE: same nominal inputs, seeded
  stochastic variation, worth sampling multiple times to characterize the
  distribution or pick a best-of-N result.
- **Combinations of the above** as a genuine design-space exploration (DSE)
  — e.g. sweeping {synthesis strategy} × {P&R seed} to find the
  best-objective candidate for #387's single scored gate — is the scenario
  under which digital work becomes "embarrassingly parallel" at all, exactly
  as Epic #391's own "Elastic compute" section frames it.

**One structural caveat, unlike a SPICE corner:** a SPICE corner (netlist +
model + process/mismatch seed) is a pure function by construction (#348's
deterministic-per-sample seeding). A digital "candidate" is only as
reproducible as its own inputs are pinned — RTL, constraint file, PDK/liberty
version, Yosys/OpenROAD version, and (for P&R) the seed, must all be fixed
for two runs of "the same candidate" to be comparable. This is a
documentation/discipline requirement for whichever Phase 2/4 issue builds the
synthesis/P&R verbs (their JSON contracts should surface these as explicit,
echoed inputs — the same "reproducibility provenance" role
`environment.remote` fields already play for `klt sim`), not a blocker to
this decision.

**Sub-decision: pipeline stages within one candidate are not independently
parallelizable across hosts.** Synthesis must complete before P&R can run on
its netlist (and, depending on where a Phase 3 functional-verification step
is inserted, before or after synthesis) — that is a serial data dependency
*within* one candidate, not a place for fleet-level fan-out. Parallelism
under this epic is strictly **across candidates**, never across a single
candidate's own stages. This is consistent with Epic #391's own contract
design (three separate JSON contracts — synthesis / place-and-route /
functional-verification — composed serially per candidate, per #399), and
requires no change to that contract-separation decision.

## Question 2: can #375's scheduler be re-parameterized around "candidate," or does it need a materially different shape?

**Decision: extend #375 in place. No rewrite.** Walking the same
implementation pieces enumerated in "Grounding" above:

| #375 piece | Reusable for digital as-is? | Why / what changes |
| --- | --- | --- |
| `FleetLauncher`/`run_fleet` launch-provision-teardown lifecycle | Yes, unchanged | Never inspects unit content; only orchestrates "launch K hosts, run a callable on each, tear down all." |
| `require_fleet_cost_gate` (fleet-level cost ceiling) | Yes, unchanged | Operates on `hosts × instance_type` rate only. |
| `check_vcpu_quota` (vCPU quota pre-check) | Yes, unchanged | Operates on `hosts × instance_vcpu_count(instance_type)` only. |
| One-retry-per-shard (`_run_shard_with_retry`) | Yes, unchanged | "This shard's run raised, relaunch and retry once" has no unit-shape dependency. |
| Teardown-all guarantee (`__exit__`/`terminate_all`) | Yes, unchanged | Same reasoning. |
| `remote_transport.JobDescription`/push-run-pull | Yes, unchanged | Already data-driven (`command`, `inputs`, `artifacts_relative_dir`); a candidate's job just uploads RTL/constraints instead of a netlist and runs `klt synth ... && klt pr ...` instead of `klt sim ...`. |
| `shard_unit_counts`'s **sizing formula** (`select_instance_type(unit_count, threads_per_unit)`) | **No — needs a new, digital-specific sizing function** | The formula's shape (`unit_count × threads_per_unit → vCPU need`) encodes "many lightweight units packed per host" (SPICE's `m > 1` slots/host). A digital candidate is the opposite: one long job that wants most/all of a host's cores for OpenROAD's own internal multi-threading (global routing, timing analysis), not one thread among many. A new sizing function (e.g. keyed off a design-size proxy, or a fixed/configurable instance tier per PDK) replaces the corner formula for this caller — same *kind* of function (unit description → instance type), different formula, not a scheduler change. |
| Merge/report assembly | **New, but already lives outside the scheduler** | #375's own docstring places this responsibility at the caller layer (Phase 1A / `sim.py`), not inside `remote_fleet.py`. Digital's merge (ranking candidates by #387's scored objective, selecting a best-of-N, or returning all candidates' metrics) is genuinely new domain logic — but it was already designed to be an addition beside the scheduler, never a change within it. |
| "K hosts" ↔ "how many units run where" mapping | **Simpler for digital, needs no scheduler change** | SPICE nests two levels (K hosts × m slots/host). Digital's `m ≈ 1` (a candidate wants ~1 whole host), so "K hosts" maps directly to "K candidates evaluated concurrently." A caller wanting more candidates than available hosts (`candidates > K`) can already express "run several candidates sequentially on this shard" inside its own `ShardRunner` callable — `ShardRunner`'s signature (`(shard_index, launcher, public_ip) -> Any`) already permits a caller to loop over multiple candidates before returning, with no `FleetLauncher` change required. |

**Conclusion:** the reusable core is the *launch/cost-gate/quota/retry/teardown
machinery* — genuinely engine- and unit-agnostic today, proven by the
`klt sim` adopter and explicitly designed for a second adopter per
`docs/design/remote-job-description.md`. What Phase 6 must build new is
narrowly scoped to two places, both already the *designed* extension points,
not scheduler internals: (1) a digital-specific sizing function replacing the
corner-shaped `unit_count × threads_per_unit` formula, and (2) a
digital-specific `JobDescription` builder + candidate-ranking merge step,
living beside `remote_fleet.py` the same way `sim.py`'s
`_build_remote_job_description` does today — not a second scheduler.

## Question 3: does the SPICE cost-gate/timing model apply, or does digital need its own?

**Decision: the guardrail *mechanism* transfers unchanged; the *calibrated
timing model* does not, and must be re-measured for digital, not assumed.**

- **Mechanism (transfers as-is):** pre-provision cost estimate logging, the
  fleet-level `hosts × rate` ceiling, the vCPU quota pre-check, the
  idle-shutdown TTL, and the guaranteed-teardown-on-every-exit-path
  discipline are all generic "is this fleet affordable and bounded"
  guardrails — none of them are calibrated to SPICE's specific numbers, and
  all apply identically to a fleet of digital-candidate hosts.
- **Timing model (does not transfer as calibrated):** #374's model,
  `wall(K) ≈ T_o + ceil(N/(K·m))·t`, with the measured `T_o ≈ 140 s` and the
  "keep per-host compute ≥ β·T_o, β ≈ 4–5" sizing rule, was derived for and
  calibrated against SPICE's shape. Two structural reasons it does not carry
  over:
  1. **`t` is two to three orders of magnitude larger for digital.** A SPICE
     corner's `t` (~140 s including host lifecycle) is *comparable to* `T_o`
     itself, which is exactly why the β-bounded overhead-minimization rule
     matters for SPICE — without it, `T_o` can dominate `wall(K)`. A single
     synth+P&R candidate's own compute time is realistically minutes to
     hours depending on design size — `t ≫ T_o` by construction, so
     per-host overhead is a small fraction of wall-clock almost regardless
     of `K`. The overhead-minimization problem the SPICE model exists to
     solve is nearly moot for digital.
  2. **`m` (slots per host) collapses to ≈1.** SPICE's model packs several
     independent corners onto one host (`m = vCPU / threads_per_corner`,
     e.g. `m = 6` on a `c7i.12xlarge`); a digital candidate instead wants
     the whole host's cores for OpenROAD's own internal parallelism. With
     `m ≈ 1`, the SPICE model's core lever — trading host count against
     per-host packing density to minimize overhead share — has nothing left
     to optimize; K-selection for digital is closer to "K = however many
     candidates you want evaluated concurrently, bounded by cost/quota
     ceilings," not an overhead-bounded search.
  3. **No measured digital T_o/t exists yet**, and none should be assumed.
     Following the same discipline the SPICE spike itself required before
     using its own numbers (`docs/design/remote-sim-backend-spike.md`'s
     "documented estimate, not a measurement" caveat, and
     `docs/design/lvs-extraction-spike.md`'s "scoring... before anything
     exists... would be inventing a number"): Phase 6 must derive its own
     T_o/t from a first live digital fleet run (the digital-flow analogue of
     #378's role for SPICE), not import `T_o ≈ 140 s` or β≈4–5 as a
     default. Given points 1–2 above, the resulting K-selection rule for
     digital is expected to be materially simpler than SPICE's — most
     plausibly a straight affordability/quota bound rather than an
     overhead-vs-compute optimization — but that expectation itself should
     be confirmed by a first measurement, not assumed in Phase 6's design.

## Decision summary (durable statement, for linking)

1. **Unit of parallelism for digital work under Epic #391 is "one candidate
   evaluation"** — one complete synthesis(+verification)+place-and-route
   pipeline run at one design-space point (a synthesis-strategy / floorplan /
   P&R-seed combination) — never one pipeline stage, never "one corner."
2. **#375's fleet scheduler is extended in place, not rewritten.** The
   launch/cost-gate/quota/retry/teardown machinery in `remote_fleet.py` /
   `remote_launcher.py` is unit-agnostic today and needs no change. Only two
   pieces are new, and both are already the designed extension points (per
   `docs/design/remote-job-description.md`): a digital-specific instance-sizing
   function (replacing the corner-shaped `unit_count × threads_per_unit`
   formula, since digital's `m ≈ 1`), and a digital-specific
   `JobDescription` builder + candidate-ranking merge step living beside the
   scheduler the way `sim.py`'s own builder does today.
3. **The cost-gate/teardown *mechanism* transfers unchanged; the SPICE
   *timing model* (`T_o ≈ 140 s`, `wall(K)` formula, β≈4–5) does not.**
   Digital needs its own measured `T_o`/`t` from a first live fleet run before
   Phase 6 designs a K-selection rule, and that rule is expected to be
   simpler than SPICE's overhead-minimization heuristic because `t ≫ T_o` and
   `m ≈ 1` for digital.

## Linked from

- Epic #391 (Phase 1 checklist item: "Resolve the fleet-scheduler
  unit-abstraction question... a documented decision, not an
  implementation") and its "Elastic compute" / Success Criteria / Risks &
  Considerations sections, all of which point at this question.
- Issue #400 (this decision's tracking issue).

## Out of scope for this decision

No code in `remote_fleet.py`, `remote_launcher.py`, `remote_transport.py`,
or any `klt` CLI surface is touched by this document. No AWS resource is
created or modified. Phase 6's actual implementation (the new sizing
function, `JobDescription` builder, and merge/ranking logic named in
Question 2) is future work, gated on Phases 2–5 landing first per the
epic's own phase ordering, and is not started here.
