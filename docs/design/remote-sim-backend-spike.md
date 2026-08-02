# Design note: AWS provisioning for the `remote` sim backend

**Status:** design note / decision record. Nothing here authorises
implementation and no `klt` code, dependency, or AWS resource is touched by
this document. Per
[docs/ARCHITECTURE.md](../ARCHITECTURE.md) → "How capabilities arrive," a
major capability arrives by spiking a design epic first; this is Phase 1 of
[Epic #253](https://github.com/2AMLogic/klayout-tools/issues/253) (elastic
on-demand compute for `klt sim`), which restructured and superseded #168.
It follows the structure the two prior accepted spikes set —
[docs/design/spice-corner-runner-spike.md](spice-corner-runner-spike.md)
(candidate survey → proposed contract → wrap/build decision) and
[docs/design/lvs-extraction-spike.md](lvs-extraction-spike.md) (#161,
the pattern this issue's own body cites) — adapted to a provisioning
decision rather than an engine-choice decision.

**What this note does and does not settle.** #168 raised five open
provisioning questions and was rejected six times by Champion review for
carrying them unresolved into a single issue. The epic's operator decisions
(recorded 2026-08-01, in #253's body) already closed the *policy* questions
— cloud spend is permissioned, the provider is AWS, and the budget ceiling
lives in AWS-native controls, not an agent-inferred number — so this note
does not relitigate those. What remains, and what this note exists to
decide, are the five *design* forks #253 explicitly delegates here:
provisioning mechanism, granularity, warm-vs-cold, model-library transport,
and result fidelity — plus the minimal-credential host profile #168 also
specified. Phase 2's implementation issues are written directly from the
decisions below; where a question is genuinely deferred, it is deferred by
name in "Open questions for Phase 2," not left implicit.

## Grounding: what already exists to build on

Two things this note treats as fixed inputs, not decisions of its own:

- **The `run_sim` seam.** `src/klayout_tools/sim.py:224`'s `run_sim` (the
  backend-selection seam #254 landed, merged as #257) already has a
  `backend` request field/`--backend`-flag, defaulting to `local` (today's
  sequential behaviour, byte-identical) with `local-parallel` and `remote`
  already documented in `docs/cli/sim.md` as reserved-but-unimplemented
  names — `local-parallel` (#255, a bounded local worker pool over
  `_run_corner()`, not yet merged) and `remote` (this note). Per #254's own
  description, "the backend interface should take the expanded corner list
  plus resolved netlist/model paths and return per-corner results" — this
  note's `remote` backend is designed as a third implementation of that
  same interface, not a parallel code path.
- **`repo:remote`, this repo's own existing AWS provisioning tool.**
  Verified live against `.claude/skills/repo/scripts/repo-remote.sh` (790
  lines, the non-interactive implementation the `/repo:remote` skill
  wraps) and `.claude/commands/repo/remote.md` (its prose contract). This
  already proves several of the pieces `remote` needs — a cost gate that
  refuses to provision without a pinned, cost-relevant instance type
  (`require_cost_config`), a curated hourly-price table with an
  approximate fallback (`estimate_cost`, lines 139–185), and a self-installing
  cloud-init idle-shutdown guard (`idle_guard_userdata`, lines 278–318) that
  powers a host off after a configurable idle window. Decision 1 below
  evaluates reusing this tool directly, and decisions on guardrail
  mechanics below explicitly reuse its *patterns* regardless of that call.

## 1. Provisioning mechanism

**Candidates, weighed on the three axes the issue names: one provisioning
surface to maintain, teardown guarantees, and spot/preemptible support.**

### `repo:remote` (reuse as-is)

| Property | Finding |
| -------- | ------- |
| Fit | `repo-remote.sh`'s own docstring states its contract: "a reachable Ubuntu box, this repo's SSH alias written, instance id recorded" — a **named, persistent, reused** dev box. `aws_up` (line 410) first checks a pinned `REPO_REMOTE_INSTANCE_ID`, then a tagged instance already running for this repo (`aws_find_tagged`), and only creates a fresh instance if neither exists — i.e. its default behaviour is explicitly "reuse the box that's already there," the opposite of "one fresh, disposable instance per job." |
| Teardown | `down` is a separate, explicit call (`repo-remote down --yes`); nothing in `up` schedules its own termination beyond the idle-shutdown guard's minutes-scale window (default 120). There is no "guaranteed teardown when the calling process dies mid-job" primitive beyond that same idle guard. |
| Spot support | **Absent.** `aws_create`'s `run-instances` argument list (lines 386–394) has no `--instance-market-options`/spot request path. |
| Credential shape | **Absent, and structurally the wrong shape.** `aws_create` has no `--iam-instance-profile` flag at all — every instance it launches gets whatever the account's default is (typically none, but not *enforced* narrow). Its access model is SSH-key based, built for a human or agent to log in and run arbitrary commands — broader than the "netlist + models + ngspice only" profile #168 specifies. |
| Verdict | Retrofitting spot support and a scoped IAM instance profile onto `repo-remote.sh` would not be "reusing the surface" — it would be adding two capabilities its own design (a reusable named box you SSH into and leave running) has no organic use for, to a general-purpose dev-box tool used by every repo Loom manages. That risk (destabilising a shared tool for one specialised caller) outweighs the code-reuse benefit. |

### AWS Batch (managed job queue)

| Property | Finding |
| -------- | ------- |
| Fit | Purpose-built for exactly "submit a job, run it on a right-sized instance, tear the compute down when idle" — AWS manages the Compute Environment's scale-to-zero, so the "did we guarantee teardown" question moves almost entirely to AWS's own service guarantee rather than our code. |
| Teardown | Effectively free — Batch's managed Compute Environment terminates instances once its job queue drains, independent of whether our own client process is still alive. This is the strongest teardown story of the three candidates. |
| Spot support | Native — a Compute Environment can declare `SPOT_CAPACITY_OPTIMIZED` (or a similar) allocation strategy with no custom code. |
| Cost | The strongest teardown/spot story comes with the largest new surface: a container image (ngspice + curated PDK decks, per decision 4) built and pushed to ECR, a Compute Environment, a Job Queue, a Job Definition, and (at minimum) three IAM roles — a Batch service role, an ECS/EC2 instance role, and a job execution role — versus one launch template and one narrowly-scoped instance profile for a direct-EC2 path. Batch's own value proposition is multi-tenant job *scheduling* — many callers sharing a queue — which is not the shape of this problem: one `klt sim --backend remote` invocation is one job, submitted by one caller, with corner-level fan-out already owned by `run_sim`'s existing (and #255's soon-to-exist parallel) corner loop. Batch would duplicate that fan-out logic inside its own job-array semantics rather than reuse it. |
| Verdict | Genuinely the safest teardown story, but the "one provisioning surface to maintain" concern cuts *against* it here: it is not reuse of anything this repo already has, and it is a materially bigger new surface (container build, registry, three IAM roles, a managed queue) than a purpose-built EC2 launcher for a single-job-at-a-time backend. |

### Direct EC2 API calls (`RunInstances`/`TerminateInstances`, purpose-built)

| Property | Finding |
| -------- | ------- |
| Fit | A narrow, `klt`-owned launcher: `RunInstances` with `InstanceMarketOptions.MarketType=spot`, an ephemeral, narrowly-scoped IAM instance profile (or none — see the credential-shape section below), `InstanceInitiatedShutdownBehavior=terminate`, and the same idle-shutdown cloud-init guard pattern `repo-remote.sh` already proved (baked onto the AMI per decision 4, not re-derived). |
| Teardown | Two independent mechanisms: an explicit `TerminateInstances` call wrapped in the launcher's own try/finally-equivalent (covers normal completion, an exception mid-run, and a caught signal), *and* `InstanceInitiatedShutdownBehavior=terminate` plus the idle-shutdown guard as a host-side backstop that fires even if the launcher process is killed uncatchably. Belt-and-suspenders, not "trust one path." |
| Spot support | Native EC2 API capability, no added abstraction layer. |
| Cost | Smallest new surface of the three: one launch template/AMI, one instance profile (or none), one security group — no container build, no registry, no managed queue. |
| Verdict | Gets spot + guaranteed teardown + minimal credentials with the least new infrastructure, at the cost of being new code rather than literal reuse of an existing binary. |

### Recommendation

**Direct EC2 API calls, via a purpose-built launcher inside `klt`, reusing
`repo:remote`'s proven *patterns* (cost-gate logging, curated price table
with an approximate fallback, idle-shutdown cloud-init guard) rather than
calling into `repo-remote up`/`down` for a persistent named box.** This is
the SPICE-runner spike's own wrap/build split turned toward provisioning
instead of simulation: that spike separated "wrap the numerics" (ngspice,
unmodified) from "build the orchestration" (the corner-matrix machinery
"There is nothing to wrap here... This layer is ours, first-class, written
from scratch", spice-corner-runner-spike.md §3, "Wrap or build?") —
`repo:remote`'s cost-gate and idle-shutdown-guard code *are* the proven,
already-battle-tested part and are reused directly (copied/adapted, not
reinvented); the ephemeral, spot-capable, minimally-privileged per-job
launch loop around them is the orchestration this backend actually needs
and `repo:remote`'s own design does not provide.

Because the contract is engine/backend-agnostic (`request["backend"]` is a
data field, per #254), this is a reversible choice within Phase 2 without
touching `docs/cli/sim.md`'s public shape: if Batch's teardown guarantee
proves worth its larger surface once a Phase 2/3 friction log demands it,
that is a provisioning-layer swap behind the same `remote` backend name, not
a contract change.

## 2. Granularity: one instance per corner matrix

**One instance sized for the whole requested corner matrix (this issue's own
lean, inherited from #168 — ~48 vCPU for the measured 5-corner × 8-thread
case), not one instance per corner.**

- **Reuses #255's fan-out code directly.** `local-parallel` is already a
  bounded worker pool over `_run_corner()`. A per-matrix `remote` backend is
  "provision one right-sized box, then run the same worker-pool code
  `local-parallel` already implements, on that box instead of the caller's
  own cores" — no new distributed-dispatch design, no new corner-ordering
  logic, no new failure-isolation logic (a failing corner already can't
  abort its siblings, per #255's own acceptance criteria). Per-corner
  granularity would require an entirely separate N-instance
  provision/run/collect/teardown lifecycle with N times the teardown paths
  to guarantee and N times the spin-up latency paid serially or in
  parallel-provisioning complexity neither #254 nor #255 needs to solve.
- **Spin-up is a one-time, amortised cost at per-matrix granularity.**
  Whatever decision 3 estimates for cold-start latency is paid once per
  request, not once per corner — directly relevant to decision 3's
  crossover calculation below.
- **Model-library transport (decision 4) is paid once, not N times.** A
  baked-AMI or fetched-PDK cost multiplied by corner count for no benefit
  (the PDK doesn't vary by corner) is a straightforward waste at per-corner
  granularity.
- **Matches the measured case's own shape.** #168's measurement is a
  5-corner gf180 matrix, each `ngspice` process itself 8-threaded — the
  contention problem is "5 independent 8-thread-hungry processes fighting
  over one 8-vCPU box," which a single ~40+ vCPU box sized for the *whole
  matrix* solves directly; sizing per-corner (each corner getting its own
  small instance) would recreate a fragmented-fleet version of the same
  right-sizing problem instead of solving it once.

## 3. Warm vs cold: cold, per-job instances; warm pool explicitly deferred

**Cold per-job instances for v1.** #168's own proposal already states the
design constraint this decision confirms, describing the measured 5-corner
case run on a right-sized instance: "The instance exists only for the job."
A warm pool is a standing, idly-billed resource, which
is a different commitment (continuous background spend, a pool-sizing
policy, pool-staleness handling for baked model libraries) than "spend is
permissioned per job" — and no operator decision authorises standing spend
the way #253's operator decisions explicitly authorised per-job spend.

**Documented spin-up estimate (not measured — no cloud spend in this
phase).** Publicly documented EC2 characteristics: a `RunInstances` call
typically reaches the `running` state within 10–60 seconds for standard
instance families including compute-optimized `c7i`; OS boot plus
`cloud-init` completion (network up, SSH daemon ready) typically adds
another 15–45 seconds for a stock Ubuntu AMI with no first-boot package
installation. Combined with decision 4's choice to bake ngspice and the PDK
decks into the AMI (no first-boot download), a documented estimate of
**roughly 1–3 minutes from `RunInstances` to "ready to accept the first
corner"** is reasonable for v1 planning. This is a documented estimate, not
a measurement — Epic #253's own Phase 3 acceptance criterion ("end-to-end
run of the measured 5-corner gf180 matrix; record remote-vs-contended-local
wall-clock comparison") is where a real number replaces it, and Phase 2's
implementation should log the actual observed spin-up time in
`environment` (see decision 5) from the first real run onward so the
estimate self-corrects.

**Crossover heuristic below which `local`/`local-parallel` is
recommended.** Using the ~1–3 minute spin-up estimate: if the requested
corner matrix's own expected uncontended compute time is on the order of a
few minutes or less, spin-up is comparable to or larger than the compute it
would accelerate, and `local`/`local-parallel` should be preferred. Above
roughly that threshold — and *especially* under the measured contention
scenario (#168: one sweep's wall-clock degraded to 2202s at 68% efficiency
from CPU contention alone, before counting queueing behind sibling sweeps)
— `remote`'s amortised one-time spin-up is a clear win. Concretely, recommend
Phase 2 surface this as guidance in `docs/cli/sim.md` (e.g. "prefer
`local`/`local-parallel` for a single-corner smoke check; `remote` pays off
once a matrix's uncontended wall-clock would run several minutes or more, or
whenever the caller's own host is already contended") rather than a hard
enforced gate — the tool should not silently override the caller's explicit
`backend` choice, only document when each is the better call. This mirrors
how `klt sim` already treats a caller-set runtime budget: `options.timeout_s`
(default `120`, per `docs/cli/sim.md`) is respected as given, not
second-guessed or auto-tuned by the tool deciding a request "looks slow" —
guidance belongs in documentation, enforcement stays scoped to what the
guardrail mechanics below actually require (cost visibility, teardown), not
to overriding an explicit backend choice.

**Why not a warm pool now, and what would change the answer.** A short-lived
warm pool solves amortising spin-up cost at *high duty cycle* — but
`--backend remote`'s call frequency is unmeasured today (the feature does
not exist yet), so sizing a pool policy would be inventing a number, the
same failure mode the LVS spike's rewrite-rule scoring explicitly avoided
(lvs-extraction-spike.md §3, "scoring... before anything exists... would be
inventing a number"). If a Phase 3+ friction log later shows spin-up
dominating wall-clock or cost at an observed real call frequency, revisiting
this is a provisioning-layer change only: `local-parallel`'s worker-pool code
already deployed to the box (decision 2) does not care whether the box was
freshly launched or drawn from a pool, so adding a pool later needs no
contract change and no re-plumbing of the corner-execution path.

## 4. Model-library transport: bake into a maintained AMI

**Bake ngspice and the curated PDK model decks (sky130A, gf180mcu) into a
maintained, versioned AMI. Ship only the per-job netlist and request-specific
files — kilobytes to low megabytes of plain SPICE text — per job.**

- **The PDK model library is the PDK's own published, versioned, largely
  static artifact** — sky130A's and gf180mcu's ngspice decks change on the
  PDK's own release cadence, not per job. That is the textbook case for
  baking into an image rather than re-fetching a large, static dependency
  on every run.
- **Cold, ephemeral instances (decision 3) give host-side caching nothing
  to persist to.** "Cache on host" only pays off across multiple jobs on
  the *same* host; a per-job-terminated instance never gets a second job to
  amortise the cache against, so plain on-host caching degrades to
  "download every time" — all of ship-per-job's latency cost, none of
  caching's benefit.
- **The netlist genuinely is per-job and small**, and shipping it per job is
  correct and unavoidable — this is exactly what `klt extract`'s output and
  `klt sim`'s `netlist` field already are (a circuit body, typically
  kilobytes; see `docs/cli/sim.md` → "Netlist convention"). Conflating "ship
  the netlist" with "ship the PDK" would multiply a multi-hundred-megabyte
  transfer by every job for no reason.
- **A maintained AMI is a real Phase 2 deliverable**, not decided further
  here: a build/refresh pipeline that produces a published, versioned AMI
  per supported PDK combination, keyed so a request can (implicitly or
  explicitly) pin which baked PDK snapshot it wants. This note fixes the
  *shape* (bake, don't fetch-per-job, don't rely on host caching under a
  cold-instance model) and leaves the pipeline's mechanics to Phase 2.
- **Feeds decision 5 directly.** The AMI's baked PDK version/hash becomes
  part of the response's reproducibility provenance, the same role
  `models_lib_sha256` already plays for a local run — a `remote` run must
  report which baked snapshot it used so a caller can tell whether it
  matches what their own `--pdk-root` would have resolved locally.

## 5. Result fidelity: same contract, explicit provenance, no new byte-identity promise

**`remote` guarantees the same request/report JSON contract and the same
pass/fail *semantics* as `local`/`local-parallel`; it does not, and cannot,
guarantee byte-identical output, and this is not a new gap — `engine_version`
already legitimately varies host-to-host today** (`docs/cli/sim.md` → "Semantics
and guarantees": "`runtime_s` and `engine_version` are the only fields that
legitimately vary run-to-run/machine-to-machine"). `remote` widens *degree*,
not *kind*: a different host was already an accepted source of variance
before this backend existed.

**What is guaranteed identical:**

- **Same code path.** Per decision 2, `remote` runs #255's `local-parallel`
  worker-pool code against `_run_corner()` on the provisioned box — not a
  reimplementation. Corner expansion, ordering, measurement extraction,
  `pass`/`fail`/`error` classification, and `margin` sign convention are
  therefore identical logic, not merely an identical schema.
- **Value-identical measurements, given identical inputs and engine
  version.** ngspice is deterministic for a given netlist/model
  library/version — no RNG in scope (Monte Carlo is explicitly deferred,
  per spice-corner-runner-spike.md → "Room to grow without breaking"). If
  the AMI's baked PDK hash matches what a local run's `models_lib_sha256`
  would report, and `engine_version` matches, `.meas` values are expected to
  match bit-for-bit — the same guarantee two local runs on hosts with
  matching ngspice versions already have.

**What is not guaranteed, and why:**

- `runtime_s` — different hardware, already an accepted variance.
- `engine_version` — whatever ngspice build the AMI bakes; Phase 2 should
  pin the AMI to the same major ngspice version this repo already tests
  against (documented, not enforced byte-for-byte), so this is a *known*,
  not merely *possible*, source of variance.

**New provenance `remote` must add (additive fields, no schema break — see
"Proposed additive contract fields" below):** instance type, region, AMI
id/baked-PDK snapshot version, and the actual measured spin-up time. A
`remote` response without these would be strictly less reproducible than a
`local` one; a caller re-checking a stored `remote` result needs to know
*which* baked snapshot and *which* instance type produced it, the same
motivation `environment.models_lib_sha256` already serves for `local`.

## Minimal-credential host profile and IAM shape

Per #168's decided host profile: *"the sim host needs netlist + models +
ngspice only — no Claude token, no forge access."* Concretely:

- **Default: no IAM instance profile attached to the guest at all.**
  Decision 4 (bake models into the AMI) plus an SSH/SCP push-then-pull
  transport (the launcher — running with the operator's own AWS credentials,
  the same credentials `repo:remote`'s cost gate already requires — pushes
  the netlist to the instance and pulls the report/artifacts back over the
  same channel) means the **guest never needs to call any AWS API**. No
  instance profile is the tightest achievable credential shape: not "scoped
  down," but **absent**.
- **Fallback, only if Phase 2's build finds SSH-based collection unreliable
  for large artifact sets:** an IAM instance profile scoped to exactly one
  action on exactly one resource — `s3:PutObject` on a single job-scoped
  prefix (`arn:aws:s3:::<bucket>/jobs/<job-id>/*`) — nothing else: no
  `s3:GetObject` on other prefixes, no `ec2:*`, no `iam:*`, no wildcard
  resource, no cross-account access. The *launcher* retains the broad
  provisioning credentials (`ec2:RunInstances`/`TerminateInstances`/etc.);
  they never reach the guest either way.
- **No Claude token, no git/forge credentials, no SSH access to any other
  host** — the guest's entire job is "receive a netlist, run ngspice, return
  results." This is a materially smaller blast radius than a general-purpose
  Loom sweep worker or a `repo:remote` dev box (#168's own framing), and the
  design should exploit that rather than reuse a general-purpose worker
  image or IAM role.
- **Network posture.** No inbound rules except SSH from the launcher's own
  IP/CIDR (mirroring `repo:remote`'s own security-group-driven access
  model). Because decision 4 bakes everything the guest needs into the AMI,
  outbound egress can be locked down to nothing once boot completes — a
  strictly tighter posture than `repo:remote`'s general-purpose dev-box
  default, which needs broader egress for `apt`/`git`/first-boot setup
  commands the sim host never runs.

## Guardrail mechanics

Concrete enough for Phase 2 issues to implement directly, mapped to the
epic's three named mechanical guardrails:

1. **Pre-provision cost estimate logging.** Before any `RunInstances` call:
   resolve the instance type from decision 2's sizing recipe (below), look
   up its hourly price (spot and on-demand) — reusing `repo-remote.sh`'s
   curated table-with-approximate-fallback pattern (`estimate_cost`, lines
   139–185), extended with the compute-optimized (`c7i`-family) entries the
   existing table lacks — and log `estimated_hourly_cost_usd` (spot and
   on-demand) to the response's `environment.remote` block **before**
   provisioning. If the instance type cannot be resolved to a cost
   (equivalent to `repo-remote.sh`'s `require_cost_config`, lines 225–255),
   the run fails loudly with `SimError` before any AWS API call that costs
   money — never a silent default, the same discipline `require_cost_config`
   already enforces verbatim: "Missing config fails loudly (exit 2), never a
   silent default" (`repo-remote.sh` lines 220–224).
2. **Idle-TTL.** Bake the same self-installing cloud-init idle-shutdown
   guard `repo-remote.sh` already ships (`idle_guard_userdata`, lines
   278–318: a once-a-minute cron job checking load average, with an
   optional marker-file override) into the AMI, but with a **materially
   shorter window than `repo:remote`'s 120-minute dev-session default** —
   recommend on the order of 10–15 minutes, generous slack for a corner
   matrix's compute plus artifact collection, not a "come back to this
   later" allowance. This is the mechanical backstop against "the launcher
   died and never called terminate," independent of decision 3's "no
   standing warm pool" choice — an idle *job* instance should never exist
   for long regardless of pool policy.
3. **Teardown guaranteed on all failure paths.** Two independent
   mechanisms, not one: (a) the launcher wraps provision → run → collect in
   a try/finally-equivalent that calls `TerminateInstances` on normal
   completion, on any exception during the run, and on a caught
   SIGINT/SIGTERM — covering "the job failed" and "the operator interrupted
   it"; (b) `InstanceInitiatedShutdownBehavior=terminate` is set at launch
   *and* the idle-shutdown guard from (2) runs regardless of whether (a)'s
   explicit call ever arrives — covering "the launcher process itself was
   killed uncatchably or the client died." This directly satisfies Epic
   #253's success criterion "Teardown verified on success, failure, and
   client-death paths" with two paths that do not share a single point of
   failure.

## Instance sizing recipe for the measured 5-corner case

**Formula:** `vcpu_needed = corner_count × threads_per_corner` (each ngspice
process is 8-threaded, per #168's measurement — `threads_per_corner` is a
known constant of the ngspice invocation, not a guess); select the smallest
available compute-optimized instance size whose vCPU count is `>=
vcpu_needed` with roughly 20% headroom for OS/guard/artifact-collection
overhead, preferring the `c` family (`c7i` at time of writing) over general
purpose (`m`) or memory-optimized (`r`) families — ngspice is CPU-bound with
a modest memory footprint per process, so paying for extra memory-per-vCPU
buys nothing here.

**Applied to the measured case:** 5 corners × 8 threads = 40 vCPU wanted.
AWS's `c7i` family steps in vCPU count as 16 (`4xlarge`) → 32 (`8xlarge`) →
48 (`12xlarge`) → 64 (`16xlarge`); 32 is short of 40, so the recipe selects
`c7i.12xlarge` (48 vCPU) — exactly this issue's own stated lean ("~48 vCPU
for 5 corners × 8 threads"), arrived at by the general formula rather than
hand-picked for this one case.

**This generalizes #255's own worker-count default in the opposite
direction.** `local-parallel`'s worker count is bounded by the box you
already have (`os.cpu_count()` divided by an assumed threads-per-ngspice
factor, per #255's description); `remote`'s sizing recipe instead *chooses*
the box to fit the corners — the same underlying constraint (threads-per-
ngspice-process vs. available vCPUs), solved by picking the instance instead
of bounding the pool.

## AWS-native budget boundary vs. tool-side mechanical guardrails

Restating #253's operator decision explicitly, and mapping each guardrail to
its owner, because this issue's own Acceptance Criteria requires this note to
state the boundary in-band rather than leave it implicit:

| Concern | Owner | Mechanism |
| ------- | ----- | --------- |
| Absolute spend ceiling (e.g. "never spend more than $X/day across all `remote` runs") | **AWS-native** (AWS Budgets, billing alerts, IAM permission boundaries) | Configured by the operator directly in AWS, outside `klt`. No per-job dollar limit is agent-inferred or enforced in-tool — the operator explicitly rejected that shape in #253's recorded decisions. |
| Per-job cost estimate visible before the money is spent | **Tool-side, mechanical** | Guardrail mechanics §1 above: logged into `environment.remote` before `RunInstances`, refuses to proceed if the estimate can't be resolved. This is a *transparency* guarantee, not a *ceiling* — it does not stop a run, it makes the cost of running it visible and in-band in the report. |
| An instance never runs longer than the job needs it to | **Tool-side, mechanical** | Guardrail mechanics §2–3 above: idle-TTL plus dual-path guaranteed teardown. This bounds *waste*, not *spend rate* — it is the tool's job to guarantee a provisioned instance's lifetime is bounded to the job, not to decide how many jobs are allowed to run. |
| Who may provision at all (IAM permission scope, which principals can call `ec2:RunInstances`) | **AWS-native** (IAM) | Out of `klt`'s scope entirely — the launcher uses whatever AWS credentials the operator has already granted it, the same posture `repo:remote`'s own cost gate already assumes (it requires credentials to exist; it does not grant them). |

The line is therefore: **AWS owns "how much, and who," `klt` owns "don't
waste it and don't lose track of it."** Nothing in this note asks `klt` to
infer or enforce a dollar ceiling; everything in Guardrail mechanics is
scoped to visibility (cost estimate logged) and waste-bounding (idle-TTL,
guaranteed teardown) — exactly the split #253's operator decisions already
drew.

## Proposed additive contract fields

**Proposed, for Phase 2 review — not shipped, and additive only** (no
existing field renamed/removed/retyped, per `docs/json-contract.md`'s house
rule and this note's own "additive envelope" convention carried over from
both prior spikes).

### Request

```json
{
  "backend": "remote",
  "remote": {
    "provider": "aws",
    "region": "us-east-1",
    "spot": true,
    "max_hourly_cost_usd": 5.0
  }
}
```

| Field | Type | Description |
| ----- | ---- | ----------- |
| `backend` | string | Already landing via #254; `"remote"` selects this backend. |
| `remote.provider` | string | `"aws"` for v1 (present from day one so provider is data, not a code path, per the epic's own "job type is a parameter" precedent for `engine`/`backend`). |
| `remote.region` | string | AWS region to provision in. No default — an unset region is a usage error, not an inferred one (mirrors `repo:remote`'s "no silent defaults for cost-relevant fields" discipline). |
| `remote.spot` | boolean | Request a spot instance (default `true` — per this issue's own framing, "corners are trivially re-runnable, so spot is a natural fit per #168"). `false` falls back to on-demand. |
| `remote.max_hourly_cost_usd` | number | Optional caller-side sanity ceiling on the *estimated* hourly rate (not a spend ceiling — see the boundary table above); if the resolved instance type's estimated cost exceeds it, the run fails before provisioning rather than silently proceeding. |

### Response — additive `environment.remote` block

```json
{
  "environment": {
    "engine": "ngspice",
    "engine_version": "46",
    "remote": {
      "provider": "aws",
      "region": "us-east-1",
      "instance_type": "c7i.12xlarge",
      "spot": true,
      "estimated_hourly_cost_usd": 0.97,
      "ami_id": "ami-0123456789abcdef0",
      "pdk_snapshot": "sky130A-2026.06.01",
      "spin_up_s": 118.4
    }
  }
}
```

| Field | Type | Description |
| ----- | ---- | ----------- |
| `remote.provider`/`region`/`instance_type`/`spot` | string/string/string/boolean | Echo of the resolved provisioning choice — what actually ran, not just what was requested (an instance-type override or spot-to-on-demand fallback should be visible here). |
| `remote.estimated_hourly_cost_usd` | number | The pre-provision estimate from Guardrail mechanics §1, carried into the report for auditability. |
| `remote.ami_id` / `remote.pdk_snapshot` | string | Decision 4/5's reproducibility provenance — which baked image and which PDK snapshot produced this result. |
| `remote.spin_up_s` | number | Measured wall-clock from provisioning start to "ready for the first corner" — the real number that should eventually replace decision 3's documented 1–3 minute estimate. |

## Out of scope for this note

No dependency was added, no `klt` subcommand or code was written, no AWS
resource was created or modified, and no MCP surface was touched. All of
this is Phase 2/3 build work, gated on the decisions above.

## Open questions for Phase 2

- **AMI build/refresh pipeline mechanics.** This note fixes the shape
  (bake ngspice + curated PDK decks; don't fetch per job) but not the
  pipeline that produces and versions the AMI, or how a request pins a
  specific baked-PDK snapshot when more than one is current.
- **Spot-price estimation source.** Guardrail mechanics §1 proposes
  extending `repo-remote.sh`'s curated static price table; whether Phase 2
  instead queries the live Spot Price History API for a tighter estimate
  (at the cost of an extra AWS call before every provision) is an
  implementation-level tradeoff this note does not resolve.
- **Fallback from spot to on-demand.** Whether a spot-capacity failure
  should retry on-demand automatically (higher job reliability, silently
  higher cost than the logged estimate) or surface as an `error` corner
  matrix requiring the caller to retry explicitly (matches the estimate,
  less automatic) is left to Phase 2.
- **Artifact collection transport at scale.** This note recommends SSH/SCP
  push-then-pull by default (for the credential-minimalism win); if Phase
  2's build finds this unreliable for large `keep_artifacts`/`waveforms`
  payloads, the S3-fallback IAM shape above is the documented escape
  hatch, but the collection mechanics themselves are unspecified here.
- **Instance-type table maintenance.** `repo-remote.sh`'s cost table is
  hand-curated and already stale relative to current-generation compute-
  optimized families; whether `remote`'s own price table is a copy, a
  shared module, or queries AWS's Pricing API directly is a Phase 2 call.
- **Crossover heuristic calibration.** Decision 3's 1–3 minute spin-up
  estimate and the "a few minutes of compute" crossover are documented,
  not measured; Epic #253 Phase 3's own acceptance criterion (measured
  5-corner gf180 wall-clock comparison) is the point at which real numbers
  should replace them, and `docs/cli/sim.md`'s eventual guidance text
  should cite the measurement, not this note's estimate, once it exists.
