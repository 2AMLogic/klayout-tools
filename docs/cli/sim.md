# `klt sim`

Run a SPICE process/voltage/temperature (PVT) corner matrix headlessly and
report per-corner measurement pass/fail as structured data.

```
klt sim <request.json> [-o|--outdir <dir>] [--backend <name>] [--max-workers <n>] [--hosts <n>] [--budget-s <seconds>] [--resume] [--fail-fast-probe] [--plot <dir>] [--op-lint [--op-lint-corner <corner_id>]] [--format text|json]
```

This is the build carried by the accepted spike,
[`docs/design/spice-corner-runner-spike.md`](../design/spice-corner-runner-spike.md)
— read it first for the engine survey and the reasoning behind the contract
shape below. This document is the shipped contract; where the two disagree,
this document (and the code) win.

- `<request.json>` — path to a request document (see "Request" below). A
  *reference*, not inline JSON on the command line — corner matrices and
  measurement lists get long fast.
- `--outdir` — override where per-corner logs/rawfiles are written when the
  request sets `options.keep_artifacts`. Defaults to a `.klt/sim/` directory
  next to the request file (the same "next to the input" convention as `klt
  render`'s default output directory).
- `--backend` — execution backend for the corner matrix, overriding the
  request's own `backend` field when given. See "Execution backends" below.
- `--max-workers` — worker-pool size for the `local-parallel` backend,
  overriding the request's own `options.max_workers` when given. Ignored by
  `local`. See "Execution backends" below.
- `--hosts` — shard the expanded corner/Monte-Carlo unit list across this
  many hosts and merge the per-shard reports, overriding the request's own
  `remote.hosts` field when given. Defaults to `1` (today's single-host
  behaviour, byte-identical). See "Fleet sharding" below.
- `--budget-s` — overall wall-clock budget in seconds for the whole sweep,
  overriding the request's own `options.wall_clock_budget_s` when given.
  Defaults to unbounded (today's behaviour). See "Wall-clock budget, orphan
  safety, and resume" below.
- `--resume` — resume from a matching on-disk checkpoint, skipping corners a
  prior interrupted run of this same request already completed, overriding
  the request's own `options.resume` when given. See "Wall-clock budget,
  orphan safety, and resume" below.
- `--fail-fast-probe` — before dispatching any real corner, run a short
  bounded `tran` calibration slice on the grid's first corner and abort the
  whole grid up front if the measured rate implies `options.timeout_s`
  cannot plausibly cover the full analysis window, overriding the
  request's own `options.fail_fast_probe` when given. See "Timeout-budget
  preflight" below.
- `--plot` — write one self-contained, dependency-free waveform SVG per
  non-sweep signal per corner to this directory, forcing
  `options.waveforms`/`keep_artifacts` on for this run. See "Waveform
  plots" below.
- `--op-lint` — **run the per-device operating-point sanity lint instead of
  the corner sweep**, on the same request document: every MOS instance's
  off/triode region, wiring smells, and netlist hygiene. Run it first when a
  measurement misses. `--op-lint-corner` picks which corner to bias at. See
  "Operating-point lint" below.
- `--format` — `text` (default, a human-readable summary) or `json`.

## Engine

`ngspice -b` is invoked as a **subprocess, once per corner** — never
`libngspice`. Per the spike's "Invocation strategy": process-corner selection
needs a fresh `.lib` parse regardless, a hung engine must be killable without
taking down `klt`'s own process, and process fan-out is the whole parallelism
story for a future `max_parallel`. The JSON contract does not name the engine
in its *shape* — `request.engine` is a data field, not a code path — but only
`"ngspice"` is implemented in this version; an unsupported value is an
application error (exit 1).

## Execution backends

`request.backend` (overridable with `--backend`) selects how the expanded
corner matrix is run. Corners share nothing — each is a pure function of
netlist + models + corner — so every backend produces the **same report
JSON, in the same corner order**, for the same request; the backend only
changes how fast (and where) the sweep runs. An unsupported name is an
application error (exit 1), exactly like an unsupported `engine`.

| `backend`        | Behaviour                                                                                                          |
| ----------------- | ------------------------------------------------------------------------------------------------------------------- |
| `local` (default) | Runs corners sequentially, one `ngspice -b` subprocess at a time, in-process.                                       |
| `local-parallel`  | Fans the same expanded corner list across a bounded local worker pool (`concurrent.futures`) — same report, same corner order, just concurrent. |
| `remote`           | Provisions one right-sized EC2 instance and runs the *same* `local-parallel` worker-pool code on it over SSH/SCP instead of the caller's own cores (Epic #253 Phase 2). See "Remote backend" below. |
| `batch`            | Submits the *same* `local-parallel` invocation to 2am's EDA batch fleet as an S3 job contract — no `RunInstances` authority, no SSH key, diversified Spot capacity acquired by 2am's own launch script ([#2080](https://github.com/2AMLogic/klayout-tools/issues/2080)). See "Batch backend" below. |

**`local-parallel` worker count.** `options.max_workers` (overridable with
`--max-workers`) bounds the pool size. When omitted, it defaults to a
conservative estimate: `os.cpu_count() // 8`, floored to at least `1` — each
`ngspice -b` process is itself internally multi-threaded (matrix
solve/BLAS), so naive one-worker-per-corner oversubscribes a small box
immediately. A non-positive or non-integer `max_workers` is an application
error (exit 1).

**Useful on a workstation, harmful on a shared worker.** `local-parallel`
trades CPU/memory for wall-clock time — fine on a dedicated development
machine with idle cores, but a bad default on a shared CI runner or
multi-tenant build box, where an uncoordinated worker pool competes with
everything else running there. `local` remains the default **everywhere**
for exactly this reason; opt into `local-parallel` deliberately (and size
`max_workers` for the box you're actually running on), never as a blanket
default.

**Ordering and failure isolation.** `local-parallel`'s report lists corners
in the same order `local` would produce, regardless of which corner's
`ngspice` process actually finishes first — completion order never leaks
into the response. A corner that errors (timeout, singular matrix, missing
measurement, …) is reported exactly as `local` reports it and does not abort
its sibling corners; only that corner's own `status`/`diagnostics` reflect
the failure.

## Remote backend

`request.backend: "remote"` provisions one right-sized EC2 instance
(`remote_launcher.RemoteLauncher`, [#264](https://github.com/2AMLogic/klayout-tools/issues/264))
and runs the corner matrix on it, per Epic #253 Phase 2
([#265](https://github.com/2AMLogic/klayout-tools/issues/265),
[`docs/design/remote-sim-backend-spike.md`](../design/remote-sim-backend-spike.md)).
It is **the same code path as `local-parallel`, run on a different box**: the
launcher pushes the netlist and a request-specific copy of the request over
SSH/SCP, then invokes `klt sim ... --backend local-parallel` directly on the
provisioned instance (which the AMI build pipeline,
`scripts/aws/build-remote-sim-ami.sh`, bakes `klt` itself into) and pulls the
resulting report/artifacts back over the same channel — corner expansion,
ordering, measurement extraction, and pass/fail classification are never
reimplemented for `remote`; they are the literal `_run_local_parallel`/
`_run_corner` functions, executing unmodified on the remote host.

`remote_launcher.py`/`remote_transport.py` themselves are job-type-neutral:
`sim.py`'s `_build_remote_job_description` supplies what gets pushed, the
remote command, and what gets collected back as a
`remote_transport.JobDescription`, rather than either module hard-coding
`klt sim`'s own shape (Epic #253 Phase 3, #278) — see
[`docs/design/remote-job-description.md`](../design/remote-job-description.md)
for the generic contract a future `extract`/`lvs`/DRC remote backend
implements against.

```json
{
  "backend": "remote",
  "models": { "pdk": "sky130A", "lib": "libs.tech/ngspice/sky130.lib.spice" },
  "remote": {
    "region": "us-east-1",
    "key_name": "my-ec2-keypair",
    "ssh_key_path": "~/.ssh/my-ec2-keypair.pem",
    "launcher_cidr": "203.0.113.4/32",
    "spot": true,
    "max_hourly_cost_usd": 5.0
  }
}
```

| Field | Type | Description |
| ----- | ---- | ----------- |
| `models.pdk` | string, required for `remote` | Selects which baked-AMI PDK to provision (`remote_launcher.SUPPORTED_PDKS`: `sky130A`, `gf180mcu`). The remote host resolves its own baked model library from this field the same way a local run resolves one (`pdk.find_pdk`, via the AMI's own `$PDK_ROOT`) — do not pair it with an operator-local absolute `models.pdk_root`, which only exists on the caller's own machine. |
| `remote.region` | string, required | AWS region to provision in. No default — an unset region is a usage error, not an inferred one (`RemoteLaunchError` from `remote_launcher.require_cost_config`, mirroring `repo:remote`'s "no silent defaults for cost-relevant fields" discipline). |
| `remote.key_name` | string, required | AWS EC2 keypair name attached to the provisioned instance, so `remote.ssh_key_path`'s private key can authenticate. |
| `remote.ssh_key_path` | string, required | Local path to the private key matching `remote.key_name`, used for every SSH/SCP call the transport makes. |
| `remote.launcher_cidr` | string | Source CIDR (typically the launcher's own public IP, e.g. `"203.0.113.4/32"`) allowed inbound SSH — the design note's SSH-inbound-only network posture. Required unless `remote.security_group_id` names an existing group or `remote.launcher_cidrs` is given. |
| `remote.launcher_cidrs` | array of strings | Multiple source CIDRs allowed inbound SSH, one ingress rule per entry — for a caller with more than one public egress IP (e.g. an office VPN plus a CI runner's own NAT IP). May be given instead of or alongside `remote.launcher_cidr`; when both are given they are unioned (deduplicated), not one overriding the other. |
| `remote.security_group_id` | string | Reuse an existing security group instead of creating a fresh ephemeral one. |
| `remote.subnet_id` | string | Optional subnet to launch into. |
| `remote.ssh_user` | string | SSH login user for the baked AMI. Defaults to `"ubuntu"` (the base Ubuntu 22.04 image `scripts/aws/build-remote-sim-ami.sh` builds from). |
| `remote.provider` | string | `"aws"` for v1 — present from day one so provider is data, not a code path. |
| `remote.spot` | boolean | Request a spot instance. Defaults to `true` — corners are trivially re-runnable, so spot is a natural fit. `false` requests on-demand directly. |
| `remote.max_hourly_cost_usd` | number | Optional caller-side ceiling on the *estimated* hourly rate (a transparency guardrail, not a spend cap — see the design note's "AWS-native budget boundary vs. tool-side mechanical guardrails"). If the resolved instance type's estimated cost exceeds it, provisioning fails before any billable AWS API call. |
| `remote.ssh_ready_timeout_s` | number | Overall budget waiting for SSH to become reachable after the instance reaches `running`. Defaults to 600s (raised from an original 240s after a live run observed cold boots — AMI first-boot cloud-init, not just the instance reaching `running` — take longer than that on some instance types/regions). Raise this further if `wait_for_ssh` still times out on a cold-boot run. |
| `remote.ssh_timeout_s` | number | Overall SSH-command timeout for the remote `klt sim` invocation itself. Defaults to a conservative fully-serial-worst-case bound (`options.timeout_s * corner_count + 120`) — the provisioned box is right-sized to run every corner concurrently, so real runs finish far faster. |
| `remote.ami_manifest` | string | Explicit override path for the AMI manifest (`remote_launcher.load_ami_manifest`'s resolution order, step 1 of 4) — use this to point at an operator-built manifest (e.g. one `scripts/aws/build-remote-sim-ami.sh` wrote) without relying on the `$KLT_AMI_MANIFEST` env var or the user-scope/packaged fallbacks. No default: when absent, resolution falls through to `$KLT_AMI_MANIFEST`, then `~/.config/klt/remote-sim-ami-manifest.json`, then the packaged `data/remote-sim-ami-manifest.json`. |

**AMI manifest resolution order** (first hit wins, mirroring [`klt pdk
find`](pdk.md)'s documented search order): (1) `remote.ami_manifest`
(explicit — an unfound explicit path is an error, not a silent fallback),
else (2) `$KLT_AMI_MANIFEST`, else (3) the user-scope
`~/.config/klt/remote-sim-ami-manifest.json`, else (4) the packaged
`data/remote-sim-ami-manifest.json`. Steps 2–4 exist so a tool-installed
`klt` (`uv tool install` / `pipx` / `pip`) can see an operator-built AMI:
`scripts/aws/build-remote-sim-ami.sh` writes both the repo checkout's
`data/` copy *and* the step-3 user-scope copy on every successful build, so
a freshly built AMI is usable from any `klt` install on that machine
immediately — no release, and no `uv run klt` from a checkout, required.

A `remote` run's response adds an additive `environment.remote` block —
report schema is otherwise **unchanged** from `local`/`local-parallel`:

```json
{
  "environment": {
    "engine": "ngspice",
    "engine_version": "46",
    "remote": {
      "provider": "aws",
      "region": "us-east-1",
      "instance_type": "c7i.12xlarge",
      "instance_id": "i-0123456789abcdef0",
      "spot": true,
      "estimated_hourly_cost_usd": 0.9639,
      "ami_id": "ami-0123456789abcdef0",
      "pdk_snapshot": "sky130A-2026.06.01",
      "spin_up_s": 118.4
    }
  }
}
```

`remote.provider`/`region`/`instance_type`/`spot` echo what actually ran
(an instance-type override or a spot-to-on-demand fallback is visible here,
not just the request's own ask — see `remote_launcher.RemoteLauncher`'s
spot-capacity-failure retry). `estimated_hourly_cost_usd` is the guardrail
mechanics cost-gate estimate, carried into the report for auditability.
`ami_id`/`pdk_snapshot` are decision 4/5's reproducibility provenance — which
baked image and which PDK snapshot produced this result, the same role
`environment.models_lib_sha256` plays for `local`/`local-parallel`.
`spin_up_s` is the measured wall-clock from provisioning start to the
remote host being ready to accept the first corner (instance reaching
`running` plus SSH becoming reachable) — `runtime_s`/`engine_version` remain
the only fields that legitimately vary between a `local` and a `remote` run
of the same request (see "Semantics and guarantees" below); given a matching
`engine_version` and PDK snapshot hash, `.meas` measurements are expected to
match bit-for-bit.

**Transport: SSH/SCP push-then-pull, no IAM instance profile on the
guest.** `remote_launcher.select_instance_type()` sizes one instance for the
whole requested corner matrix (`corner_count * threads_per_corner`, ~20%
headroom, smallest fitting `c7i` size — the 5-corner × 8-thread case selects
`c7i.12xlarge`, 48 vCPU). `remote_launcher.resolve_ami()` resolves a
`(pdk, region)` pair against the versioned AMI manifest
(`data/remote-sim-ami-manifest.json`, schema at
`docs/schemas/remote-sim-ami-manifest.schema.json`) produced by
`scripts/aws/build-remote-sim-ami.sh` — ngspice, the curated `sky130A`/
`gf180mcu` model decks, and `klt` itself are baked into the AMI, never
fetched per job. Only the netlist and a generated request document
(kilobytes to low megabytes) are pushed per job, by
`klayout_tools.remote_transport` — no IAM instance profile is attached to
the guest by default (baked AMI + SSH/SCP transport means the guest never
calls an AWS API); network posture is SSH-inbound-only from the launcher's
own IP/CIDR, with outbound egress revoked once the security group is
created. `options.keep_artifacts`/`waveforms` are collected back the same
way, over `scp -r`. An idle-shutdown guard (~12 minutes, materially shorter
than `repo:remote`'s 120-minute dev-session default) is baked into the AMI
itself. `remote_launcher.RemoteLauncher` is a context manager that
guarantees `terminate-instances` runs on normal completion, any exception
raised inside the block (including a transport failure), or a caught
SIGINT/SIGTERM — plus `InstanceInitiatedShutdownBehavior=terminate` at
launch as a host-side backstop independent of whether that explicit call
ever arrives. See `src/klayout_tools/remote_launcher.py`'s and
`src/klayout_tools/remote_transport.py`'s module docstrings for the full
mapping from these mechanics to
`docs/design/remote-sim-backend-spike.md`'s decisions.

**When to prefer `remote` over `local`/`local-parallel`.** Spin-up is a
one-time, amortised cost per request (roughly 1-3 minutes, documented not yet
measured — see the design note's decision 3), so `remote` pays off once a
matrix's uncontended wall-clock would run several minutes or more, or
whenever the caller's own host is already contended; prefer
`local`/`local-parallel` for a single-corner smoke check.

**Worked example: [`examples/sim-remote/`](../../examples/sim-remote/README.md).**
Epic #253's own closing validation, committed end to end — a 31-stage sky130
ring oscillator (`ring_tb.spice`, ~2.3 minutes of ngspice per corner) plus
two requests, `matrix-local.request.json` and `matrix-remote.request.json`,
that declare the *same* 5-process-corner matrix and differ only in `backend`
(and the `remote` block above). Both runs' reports are committed alongside
them, so the additive `environment.remote` block and the corner-for-corner
equality of the two backends are readable without provisioning anything;
*running* the remote one needs AWS credentials plus the placeholder
`remote.key_name`/`ssh_key_path`/`security_group_id` fields filled in from
the field table above. The example's README carries the measured wall-clock
comparison and the run's cost.

## Batch backend

`request.backend: "batch"` submits the corner matrix to
[2AMLogic/2am](https://github.com/2AMLogic/2am)'s EDA batch fleet as an S3
**job contract** instead of provisioning an instance itself
([#2080](https://github.com/2AMLogic/klayout-tools/issues/2080), the consumer
half of 2am#117). Like `remote`, it is **the same code path as
`local-parallel`, run on a different box** — the submitted job command is a
plain `klt sim ... --backend local-parallel --format json` invocation on the
fleet's own pinned image, so corner expansion, ordering, measurement
extraction, and pass/fail classification are never reimplemented for it.

What differs from `remote` is *who acquires the machine*:

| | `remote` | `batch` |
| --- | --- | --- |
| Capacity | This process calls `RunInstances` itself (Spot by default, one AZ/type) | 2am's `batch-fleet-provision.sh launch` calls `CreateFleet` across a diversified AZ × instance-type pool |
| Credentials here | AWS credentials with `RunInstances` + an SSH private key on every host that runs a sim | An `aws` CLI profile name (`batch-runner-submit`) — S3 put/get only; no key material, no EC2 authority |
| Transport | SSH/SCP push-then-pull | `s3://<bucket>/jobs/<job-id>/` (`job.json`, `inputs/`, `status.json`, `outputs/`) |
| Spot interruption | The whole request fails | `status.json` reports `interrupted`; 2am's scheduled `reconcile` re-launches the job and this client simply keeps waiting |
| Spend guardrail | A caller-side `max_hourly_cost_usd` estimate | The fleet's own shared concurrency cap and budget, enforced inside `launch` before any AWS call |

```json
{
  "backend": "batch",
  "models": { "pdk": "sky130A", "lib": "libs.tech/ngspice/sky130.lib.spice" },
  "batch": {
    "bucket": "<your-batch-jobs-bucket>",
    "region": "us-east-1",
    "profile": "batch-runner-submit",
    "provision_script_path": "~/GitHub/2am/infra/aws/batch-fleet-provision.sh",
    "poll_interval_s": 30,
    "poll_timeout_s": 5400
  }
}
```

| Field | Type | Description |
| ----- | ---- | ----------- |
| `batch.provision_script_path` | string, required (or `$KLT_BATCH_PROVISION_SCRIPT`) | Path to 2am's `infra/aws/batch-fleet-provision.sh` on *this* host. Resolution order: this field, then `$KLT_BATCH_PROVISION_SCRIPT`, then an error naming both — never a guessed path into a sibling checkout. An unresolvable script is an error raised **before any S3 write**, mirroring `remote.ssh_key_path`'s own early check. |
| `batch.bucket` | string, required (or `$KLT_BATCH_JOB_BUCKET`, or the fleet config) | The job bucket. Resolution order: this field, then `$KLT_BATCH_JOB_BUCKET`, then `BATCH_JOB_BUCKET` in the `batch-fleet.env` sitting beside the resolved provision script (2am's own fleet config, which is the value its IAM policy is bound to). The deployed bucket name is read from that fleet config at runtime and is deliberately not reproduced in this repo — it encodes deployment-specific account detail, so every example here shows the `<your-batch-jobs-bucket>` placeholder instead. No literal default is compiled into `klt`: an unresolvable bucket is an error naming all three sources. |
| `batch.jobs_prefix` | string | S3 key prefix jobs live under. Defaults to the fleet config's `BATCH_JOBS_PREFIX`, else `"jobs"`. |
| `batch.region` | string | AWS region passed to both the `aws` CLI and `launch`. Defaults to `$KLT_BATCH_REGION`, else the fleet config's `BATCH_REGION`, else the `aws` CLI's own resolution. |
| `batch.profile` | string | `aws` CLI **profile name** the submit path runs under — a name, never a credential; the key it resolves to lives in the operator's own AWS config. Defaults to `$KLT_BATCH_PROFILE`, else the fleet config's `BATCH_SUBMIT_PROFILE`, else `"batch-runner-submit"`. |
| `batch.poll_interval_s` | number | How often `status.json` is re-read. Defaults to `30`. |
| `batch.poll_timeout_s` | number | Wall-clock budget waiting for a terminal `status.json`. Defaults to the fully-serial worst case (`options.timeout_s × corner count + 120`) plus 1800s of Spot-acquisition/boot slack. On overrun every unit is reported with a `batch_poll_timeout` diagnostic (the job may still be running on the fleet — see "Failure classification" below). |

**The seam is 2am's, verbatim** (`infra/aws/batch-fleet.md` §"The seam: what
a consumer-repo wrapper does"), and `klt` implements exactly its four steps:

1. **write** `inputs/netlist.cir` + `inputs/request.json`, then
   `job.json` — uploaded last, so a launch can never observe a job spec
   whose inputs are still uploading.
2. **launch** `batch-fleet-provision.sh launch --job <job-id> --apply
   --profile <profile>`, exactly once per job. `klt` shells out rather than
   issuing its own `CreateFleet`: `launch` enforces the diversification
   floors (≥3 AZs, ≥3 instance types, refused before any AWS call), the
   live subnet/AZ pool cross-product, the capacity-shortfall retry, and the
   shared-budget concurrency cap. Re-deriving that here would duplicate
   already-tested logic; skipping it would be a safety regression.
3. **poll** `status.json`. `running` and `interrupted` both mean *wait* —
   an `interrupted` job is re-launched by 2am's own scheduled `reconcile`,
   never re-submitted by this client (that would double-spend the shared
   fleet budget). `done`/`failed`/`timeout` are terminal.
4. **collect** `outputs/`, read `report.json` out of it, and return it
   through the same reducer the `remote` backend uses — so a `--format json`
   report from `batch` is structurally identical to `local`/`remote`'s.

**Why the job command redirects to a file.** 2am's harness merges *all*
stdout/stderr — its own log lines and the job command's — into one
`harness.log`, so stdout is not a clean channel back to the submitter (only
`outputs/**` and `status.json` are). The generated `job.json` therefore ends
in `> "$EDA_OUTPUT_DIR/report.json"`, and `--outdir "$EDA_OUTPUT_DIR/artifacts"`
is added when `options.keep_artifacts` is set so per-corner logs/rawfiles
land inside the one tree the harness collects. The generated document's
schema is [`docs/schemas/batch-job-spec.schema.json`](../schemas/batch-job-spec.schema.json).

**`options.resume` is rejected for `batch`**, exactly as for `remote`: the
corners come back from an independently-run job via a report this process
did not construct, so there is no local `corner_id` to reconcile a
checkpoint against. The dispatch-time fail-fast probe is likewise skipped —
probing *this* host's ngspice says nothing about the fleet instance's
hardware.

**`hosts > 1` submits one job per shard** (see "Fleet sharding" below), with
the same contiguous-slice semantics, the same deterministic merge back into
global unit order, and the same `environment.remote.fleet[]` array a sharded
`remote` run produces. A shard whose job never returns is reported unit by
unit with a `lost_shard` diagnostic rather than aborting its siblings.

A `batch` run's response fills the same additive `environment.remote` slot
`remote` does, carrying what `status.json` actually observed:

```json
"remote": {
  "provider": "aws-batch-fleet",
  "job_id": "klt-sim-4f2b19c0ae31",
  "bucket": "<your-batch-jobs-bucket>",
  "region": "us-east-1",
  "instance_id": "i-0abc123",
  "instance_type": "c7i.4xlarge",
  "availability_zone": "us-east-1b",
  "ami_id": "ami-0abc123",
  "lifecycle": "spot",
  "spot": true,
  "state": "done",
  "exit_code": "0",
  "concurrency": "2",
  "physical_cores": "8",
  "elapsed_seconds": 412
}
```

**Prerequisites live in 2am, not here.** The batch AMI bake, the job
bucket's `provision --apply`, the `batch-runner-submit` access key, and the
scheduled `reconcile` timer are all 2am-side operator steps. The backend and
its tests need none of them (every AWS/launch call goes through an
injectable command runner — no test in this repo touches AWS or the
network), but a *live* run does.

**Worked example: [`examples/sim-batch/`](../../examples/sim-batch/README.md).**
The same 5-corner ring-oscillator matrix as `examples/sim-remote/`, with
`backend: "batch"` — the live-run report slot is deliberately empty until
those 2am prerequisites are done.

## Fleet sharding (`remote.hosts`)

`request.remote.hosts` (overridable with `--hosts`, same precedence rule as
`backend`/`--backend`: the CLI flag wins when given, otherwise the request
field, otherwise `1`) shards the **expanded unit list** — corners × Monte
Carlo samples, the same list `corners.*`/`monte_carlo` expansion already
produces — into that many contiguous slices and merges the per-shard
reports back into one report, in the exact single-host shape (Epic #375
Phase 1A, [#376](https://github.com/2AMLogic/klayout-tools/issues/376)).
`hosts` absent or `1` is the exact pre-existing code path — byte-identical,
not just equivalent.

- **Contiguous, order-preserving shards.** Shard `i` gets
  `len(units) // hosts` units (the first `len(units) % hosts` shards get one
  extra), always a contiguous slice of the expanded list in its original
  order — concatenating every shard reproduces the unsharded unit list
  exactly, for any `hosts` from `1` to `len(units)` (and beyond: a `hosts`
  greater than the unit count simply leaves the trailing shards empty).
  Per-sample Monte Carlo seeds are already absolute (`sample_index`-derived,
  see "Monte Carlo sampling" above and #348), so slicing never changes any
  unit's own value, only which shard runs it.
- **Deterministic merge, any completion order.** Shards run concurrently —
  one shard is meant to be one host — and are reassembled by shard index
  once every shard completes, **never** by completion order, so the merged
  `corners[]` is always in the same global unit order `hosts: 1` would
  produce. This is the same ordering contract `local-parallel` already
  honors for individual corners, one level up.
- **Lost-shard semantics.** A shard that never returns is reported as a
  per-unit `status: "error"` for every unit it owned, with a `lost_shard`
  diagnostic — the merged report's `errored` count reflects them, and a
  lost shard never aborts a sibling shard. (The automatic retry that avoids
  this in the common case — one retry before giving up on a shard — is
  Epic #375 Phase 1B, [#377](https://github.com/2AMLogic/klayout-tools/issues/377);
  this contract is what a retry falls back to if it is still exhausted.)
- **`environment.remote` becomes `fleet[]`.** With `hosts > 1`, a run whose
  backend actually populates `environment.remote` (see "Remote backend"
  above) reports one `fleet[]` entry per host instead of a single block —
  each entry has the exact same fields as today's single-host block, or
  `null` for a lost shard:

  ```json
  {
    "environment": {
      "remote": {
        "fleet": [
          {
            "provider": "aws",
            "region": "us-east-1",
            "instance_type": "c7i.12xlarge",
            "instance_id": "i-0123456789abcdef0",
            "spot": true,
            "estimated_hourly_cost_usd": 0.9639,
            "ami_id": "ami-0123456789abcdef0",
            "pdk_snapshot": "sky130A-2026.06.01",
            "spin_up_s": 118.4
          },
          null
        ]
      }
    }
  }
  ```

  A `hosts: 1` run (or any backend that never populates
  `environment.remote`, e.g. `local`/`local-parallel`) keeps the existing
  non-array shape — `fleet[]` never appears there, for compatibility.
- **`local`/`local-parallel`: in-process sharding.** `local` and
  `local-parallel` consume the expanded unit list directly (no separate
  "request document" to slice), so `hosts > 1` fans it across a thread pool
  in this same process, with no AWS-facing code at all.
- **`backend: "remote"`: a real K-instance fleet.** `hosts > 1` with
  `backend: "remote"` provisions `hosts` real EC2 instances through
  `remote_fleet.run_fleet` (Epic #375 Phase 1B,
  [#377](https://github.com/2AMLogic/klayout-tools/issues/377), wired into
  `klt sim`'s own dispatch by
  [#906](https://github.com/2AMLogic/klayout-tools/issues/906)) — the same
  K-instance launch, fleet-level cost gate, vCPU quota pre-check, one-shard
  retry, and guaranteed teardown-all `#377` already ships. `hosts` may not
  exceed the number of units to dispatch (an idle fleet member would still
  be billed). Each shard is pushed **its own** already-expanded,
  already-seeded slice of the unit list — never re-derived on the remote box
  from `corners`/`monte_carlo` ranges, which would risk two shards
  disagreeing about which points are whose, or a shard's remote box deriving
  a different seed than an unsharded run of the same request would have (its
  own `corner_index`, used in seed derivation, would otherwise be relative
  to that shard alone). `environment.remote.fleet[]` reports one entry per
  host exactly as above, plus an `attempts` field (`1`, or `2` if that
  shard's automatic retry fired).
- **`backend: "batch"`: one fleet job per shard.** `hosts > 1` with
  `backend: "batch"` submits `hosts` independent S3 job contracts
  ([#2080](https://github.com/2AMLogic/klayout-tools/issues/2080)) — the
  same contiguous slices, pushed as each job's own `_explicit_points`, and
  the same deterministic merge and `fleet[]` reporting. The difference from
  `remote` is only *who acquires the machine*: 2am's fleet schedules each
  job on its own diversified Spot capacity, so there is no K-instance
  launch, cost gate, or teardown to run here. `hosts` may not exceed the
  unit count, for the same reason. A shard whose job fails to submit,
  launch, or collect is a `lost_shard` exactly as above; a shard whose job
  *ran* and failed reports `batch_job_failed`/`batch_job_timeout` per unit
  instead (see "Failure classification" below).

## Wall-clock budget, orphan safety, and resume

Issue [#473](https://github.com/2AMLogic/klayout-tools/issues/473): agents
doing corner sweeps kept hand-rolling driver scripts into gitignored scratch
space because nothing committed enforced limits a caller could not
accidentally omit. One such driver ran 8 concurrent `ngspice` at ~95% CPU
each for 5h52m on an 8-core host — a per-corner `options.timeout_s` alone
never bounds the *whole sweep*, and killing the leaf `ngspice` processes did
nothing because the driver's own launch loop simply kept marching through
the matrix after its launcher had already exited. `klt sim` now closes all
three gaps directly, as an extension of `local`/`local-parallel` (and any
`hosts > 1` shard built on them) rather than a separate tool.

- **`options.wall_clock_budget_s` (`--budget-s`)** bounds the whole sweep's
  wall-clock time, overriding the request field when given (same precedence
  rule as `--max-workers`/`--hosts`). Distinct from `options.timeout_s`,
  which only bounds one corner: a driver with a generous per-sim timeout can
  still run for hours by simply having a long matrix. Checked **before**
  dispatching each corner, never mid-corner — a corner already running when
  the budget is hit is left to finish (bounded by its own `timeout_s`), but
  no new corner is launched. Every corner that never got the chance to start
  is reported with `status: "error"` and a `budget_exceeded` diagnostic, and
  `environment.budget` summarises the outcome:

  ```json
  {
    "environment": {
      "budget": {
        "wall_clock_budget_s": 1800,
        "elapsed_s": 1800.04,
        "exceeded": true,
        "corners_skipped": 6
      }
    }
  }
  ```

  Present only when the request declares a budget. Omitting it is exactly
  today's behaviour: unbounded, same as before this issue. A budget smaller
  than a single corner's own runtime still attempts (and, if it completes in
  time, passes) the first corner — the check happens once per dispatch slot,
  not as a pre-flight gate that could report zero corners for a legitimately
  tiny budget.

- **Orphan safety is always on, no configuration required.** `klt sim`
  captures its own parent PID once, at the start of the sweep, and the
  dispatch loop re-checks `os.getppid()` before launching each new corner.
  The instant the launching process exits, the kernel reparents this
  process to init/systemd — the next check sees a different PPID and stops
  dispatching, exactly the same mechanism the budget uses, just with an
  always-true trigger condition instead of a clock. No heartbeat file,
  lease, or external supervisor is needed. Every corner that never started
  because of this is reported with an `orphaned` diagnostic, and
  `environment.orphaned: true` is set (present only when it actually fired).
  An already-running corner is not killed the instant the parent dies — it
  is still bounded by its own `timeout_s`, same as the budget case.

- **`options.resume` (`--resume`)** persists a checkpoint of completed
  corner reports to `checkpoint.json` under `--outdir` (the same directory
  `options.keep_artifacts` writes per-corner logs/rawfiles into) as the
  sweep runs, and skips any corner already recorded there on a later
  invocation of the **same** request — this is what makes a short budget an
  acceptable default rather than a reason to avoid one. "Same request" is
  enforced by a SHA-256 fingerprint over the netlist/model library content,
  analysis, measurement cards, per-corner timeout, engine, and the exact
  expanded corner-ID list; a checkpoint whose fingerprint no longer matches
  (netlist edited, corner matrix changed) is treated as if it did not exist
  — never silently reused against a since-modified request. The checkpoint
  file is removed once a sweep finishes with nothing skipped. Currently
  implemented for `local`/`local-parallel` only (including any `hosts > 1`
  shard on them) — pairing `options.resume` with `backend: "remote"` is a
  clear application error today (exit 1): a `remote` shard's dispatch loop
  runs on the provisioned box(es), outside this process, so there is no
  local checkpoint to write against.

  ```json
  {
    "environment": {
      "resume": {
        "resumed_corners": 6,
        "checkpoint_path": { "path": ".klt/sim/checkpoint.json", "scope": "repo" },
        "checkpoint_retained": false
      }
    }
  }
  ```

  `checkpoint_path` is the `{path, scope}` shape — see "JSON schema (the
  contract)" below (issue #1261).

  Present only when `options.resume`/`--resume` was requested.
  `resumed_corners` counts corners reused from the checkpoint rather than
  recomputed this run; `checkpoint_retained` is `false` once a run finishes
  with every corner accounted for (the file is then deleted).

## Timeout-budget preflight

Issue [#1686](https://github.com/2AMLogic/klayout-tools/issues/1686): a
per-corner `options.timeout_s` protects against a *hang* (see "Failure
classification" above), but nothing protects against a *budget that can
never be met* — a real incident widened a `tran` analysis window 33× while
raising `timeout_s` only 6×, and every one of 45 corners came back
`timeout` after burning its full budget with no usable result, for ~45
wall-clock hours of saturated cores before anyone noticed the shape.

Before the grid starts, `klt sim` runs a coarse, advisory-only sanity check
on `options.timeout_s` against a `tran` analysis's own declared step/window
(scoped to `kind: "tran"` — `op`/`dc`/`ac` have no "window of simulated
time" concept). It compares `timeout_s` against the number of timepoints
implied by `window / step`, under a deliberately generous floor (50
timepoints/second — **not** a real per-engine throughput estimate; ngspice's
actual rate depends heavily on circuit size/complexity and is not knowable
without running it, and can be *orders of magnitude* slower than this floor
in practice, as the incident above shows). When `timeout_s` looks
implausible even under that generous floor, the response carries an
advisory string:

```json
{
  "environment": {
    "timeout_preflight_warning": "options.timeout_s (1s) looks implausible for a tran analysis with ~1e+05 timepoints (1n step, 100u window) -- even 50 timepoints/s (a generous floor, not a real engine-rate estimate) would need 2e+03s per corner. This is only a coarse, advisory pre-grid heuristic -- it does not block the sweep, and a plausible-looking budget can still turn out to be too short in practice."
  }
}
```

Present only when the heuristic actually has something to say — absent for
the common case. **This is advisory only: it never blocks the sweep**, and a
budget that looks "plausible" under this coarse, generous floor can still be
wildly insufficient in practice (as the motivating incident's own numbers
show) — this heuristic catches only the most obviously-impossible requests,
it is not a substitute for verifying a real campaign's actual throughput.

A stronger, dispatch-time guard that aborts a grid once early corners prove
the budget itself (not one slow corner) is the problem was scoped by the
same issue. Recovering how far a *dispatched* corner's simulated time
actually got when it is killed by its own `options.timeout_s`
(`reached_s`) remains unimplemented: every mechanism investigated for that
turned out to be unsafe or non-functional against this module's actual
per-corner deck shape (ngspice's `-r` streaming rawfile is silently
disabled by the `.control` block every real corner deck uses for its
`alter` supply-override cards; a `stop`/`resume` checkpoint workaround
around that corrupts `.meas` evaluation for ordinary, non-timeout sweeps).
**A corner that actually runs and times out still reports `reached_s:
null`** in its `timeout` diagnostic — that gap has not closed.

Issue [#1694](https://github.com/2AMLogic/klayout-tools/issues/1694) ships
the dispatch-time fail-fast guard itself via a different mechanism than
that blocked per-corner recovery: a **two-pass calibration probe**, opt-in
via `options.fail_fast_probe` (`bool`, default `false`) or `--fail-fast-probe`.
See [`docs/design/sim-corner-reached-s.md`](../design/sim-corner-reached-s.md)
for the full design decision.

### Two-pass fail-fast probe (`options.fail_fast_probe`)

When enabled, before any real corner is dispatched, `klt sim` runs one
short, bounded `tran` slice of the grid's *first* corner (same `alter`
cards and `.control` wrapper as a real corner, a much shorter window, no
`.meas` cards) to measure that corner/deck's own
simulated-seconds-per-wall-clock-second rate. That measured rate is used to
estimate whether `options.timeout_s` can plausibly cover the *full*
analysis window — the direct, evidence-based counterpart to the static
timepoints-per-second heuristic above. If the estimate says the full run
would need more than 1.5x `timeout_s`, the **entire grid is aborted**
before a single real corner runs, and the response looks like this:

```json
{
  "environment": {
    "fail_fast_probe": {
      "corner_id": "tt/27C",
      "probe_window_s": 2e-08,
      "probe_timeout_s": 10.0,
      "probe_wall_s": 10.0,
      "measured_rate_s_per_s": 2e-09,
      "rate_is_upper_bound": true,
      "analysis_window_s": 1e-06,
      "timeout_s": 100.0,
      "estimated_wall_s": 500.0,
      "estimated_reached_s": 2e-07,
      "estimated_fraction": 0.2,
      "abort_margin": 1.5,
      "abort": true
    }
  },
  "corners": [
    {
      "corner_id": "tt/27C",
      "status": "error",
      "diagnostics": [
        {
          "severity": "error",
          "code": "timeout_budget_unreachable",
          "message": "the two-pass fail-fast probe (issue #1694) measured 2e-09s of simulated time per wall-clock second at corner 'tt/27C' (an upper bound -- the probe itself was killed at its own bounded timeout before finishing its calibration slice), implying the full 1e-06s tran window would need an estimated 500s of wall-clock time per corner -- more than 1.5x the configured options.timeout_s (100s). Aborting the whole grid now rather than letting every corner burn its full timeout for a partial result -- see docs/design/sim-corner-reached-s.md.",
          "reached_s": 2e-07,
          "fraction": 0.2
        }
      ]
    }
  ]
}
```

`environment.fail_fast_probe` is present whenever the probe ran and came
back conclusive, **whether or not it aborted** — `abort: false` still
reports the measured rate/estimate, so a passing sweep shows how much
margin it actually had. `rate_is_upper_bound: true` means the probe itself
was killed at its own (much shorter than `timeout_s`) bounded timeout
before finishing its calibration slice — the measured rate is then a
worst-case upper bound (the true rate could only be slower), which is
still sufficient to prove infeasibility. The probe is silently skipped
(no `environment.fail_fast_probe` at all, and the sweep proceeds exactly
as if `fail_fast_probe` were unset) when: the analysis isn't `kind:
"tran"`; the backend is `remote` (probing this process's own host is not a
representative sample of a provisioned remote box's hardware — see
`_run_remote`'s docstring); or the probe itself is inconclusive (`ngspice`
missing, or the probe's own shortened analysis did not converge cleanly).

**Every corner aborted this way carries an *estimated* `reached_s`/
`fraction`** on its `timeout_budget_unreachable` diagnostic, derived from
the measured rate — this is explicitly an estimate of how far that corner
*would have* gotten before `options.timeout_s` killed it, computed because
the corner was never actually dispatched, and is not to be confused with a
real per-corner progress measurement. A corner that *is* dispatched (probe
disabled, or the probe did not trigger an abort) and then times out on its
own still reports `reached_s: null`, exactly as described above — this
issue does not change that.

## Deviation from the spike

The spike's proposed response shape carries a top-level
`"schema": "klt.sim.corners/1"` field. This command instead uses the shared
envelope's `"schema_version": 1` (integer, versioned per command) per
[`docs/json-contract.md`](../json-contract.md) — the house convention that
postdates the spike and every other `klt` verb already conforms to. Nothing
else in this document's request/response shape deviates from the spike.

## Netlist convention: a circuit body, not a full deck

The `netlist` a request references must be a **circuit body** — device and
subcircuit definitions plus sources — with **no `.control`/`.end` cards of
its own**. `klt sim` generates a corner-specific wrapper deck that
`.include`s the file and appends the corner's `.lib`/`.temp` cards, the
request's verbatim `.meas` cards, and a `.control` block that `alter`s the
supply sources and runs the declared analysis. A netlist that already
carries its own `.end` (a full deck exported as-is, e.g. straight off some
schematic tools) is not supported in this version — strip the top-level
control/end cards before pointing a request at it.

## Corner axes

- **`corners.process`** (`array<string | {"name": string, "sections":
  string[]}>`, optional) — process-corner axis. Each entry is either:
  - a **bare string** — opaque `.lib` section name passed through to
    `models.lib` (e.g. sky130's `tt`/`ss`/`ff`/`sf`/`fs`, or a mismatch
    variant like `tt_mm` — no schema change needed for those). Emits a
    single `.lib <models.lib> <name>` card.
  - a **bundle object** `{"name": str, "sections": list[str]}` — a named
    corner backed by *multiple* `.lib` sections, emitted as one `.lib
    <models.lib> <section>` card per entry in `sections`, **in declaration
    order** (ordering matters when the section set has interdependent
    global switch params — see gf180mcu below). `name` is what shows up as
    `corner_id`/the response's `process` field and what `corners.exclude[].process`
    matches against; it need not equal any of the section strings.

  Selecting a process corner needs a model library; `models.lib` is only
  required when this axis is present. Omit `corners.process` entirely for a
  request that doesn't care about process (single point, no `.lib` card
  emitted).

  The bundle form exists because not every PDK's vendor model deck ships a
  single all-device section per named corner. **sky130**'s
  `sky130.lib.spice` does (`tt`/`ss`/`ff`/`sf`/`fs` each covers every device
  family), so a bare string suffices there. **gf180mcu**'s
  `sm141064.ngspice` does not: it has no all-device corner sections at
  all — a named corner is a *bundle* of per-device-family sections (MOS
  `typical`/`ff`/`ss` plus `bjt_*`/`diode_*`/`res_*`/`moscap_*`/`mimcap_*`),
  so a gf180mcu `"ss"` corner needs the bundle form:

  ```json
  "corners": {
    "process": [
      "typical",
      { "name": "ss", "sections": ["ss", "bjt_ss", "diode_ss", "res_ss", "moscap_ss", "mimcap_ss"] }
    ]
  }
  ```

  A bare string and a bundle object may appear side by side in the same
  `corners.process` array (as above); a single-section bundle
  (`{"name": "tt", "sections": ["tt"]}`) is functionally equivalent to the
  bare string `"tt"`.

  **Known-good mismatch mechanisms (per-device variation):** Until full Monte
  Carlo orchestration lands, hand-rolled MC harnesses can reference the
  validated per-device-variation mechanism for each PDK's vendor model deck.
  Note the two PDKs differ structurally — sky130 selects a mismatch `.lib`
  section, while gf180mcu toggles a model parameter:

  - **sky130A**: `tt_mm` is confirmed to resolve correctly in
    `libs.tech/ngspice/sky130.lib.spice` and produces plausible per-device
    variation (validated by sky130-bandgap's `pnp-mismatch` simulation harness).
    The `_mm` suffix pattern extends to other process corners (`ss_mm`, `ff_mm`,
    etc.), following the same validation principle: the section must exist in
    the model library and produce trustworthy device-level parameter spread
    over multiple Monte Carlo samples.
  - **gf180mcu**: Mismatch works differently — there is **no** corner-suffixed
    `_mm` section to select. `sm141064.ngspice` (`libs.tech/ngspice/`) exposes
    only plain corner sections (`typical`, `ss`, `ff`, `sf`, `fs`, plus
    per-device-family variants like `res_typical`/`bjt_typical`), and every one
    of them pulls in the same internal mismatch includes — `fets_mm` for MOS
    devices and `bjt_mc` for BJTs. Per-instance mismatch is enabled by the
    model-level netlist parameter `sw_stat_mismatch` (`0` = off, `1` = on; it
    scales the per-device `delvto`/`mulu0` terms), with global
    process-statistical spread gated by the companion `sw_stat_global`
    parameter. Because these are `.param` switches baked into the corner —
    **not** selectable `.lib` section names — you set them directly in the deck;
    `corners.process`, which only chooses a section name, cannot toggle them.
    gf180-bandgap's `mc-untrimmed` harness validates exactly this path: it loads
    the plain `typical`/`res_typical`/`bjt_typical` sections and turns mismatch
    on per run via `sw_stat_mismatch: 0|1`, never by pointing `corners.process`
    at an `_mm` section.

    **The resistor family is the exception: `sw_stat_mismatch` does not
    enable it.** The poly-resistor subcircuits carry the per-instance
    mismatch hook — body resistance multiplied by `(1 + mis_r *
    sw_stat_mismatch)` — but `mis_r`'s default is a hardcoded `0`, and the
    accompanying Pelgrom-style sigma formula (`var_r = ... / sqrt(par *
    r_l * r_w)`, `mis_r = agauss(0, var_r, 1)`) ships **commented out** in
    the vendored deck. A run with `sw_stat_mismatch: 1` samples real
    variation for MOS and BJT devices and **none** for resistors, with
    nothing in the request, deck, or log distinguishing that from "this
    family contributes negligibly" (issue #355). The disabled
    coefficient's numeric value is also undocumented — no published PDK
    source states it is the intended value or under what conditions it was
    extracted; a caller who enables it by overriding the subcircuit
    parameter from the instance line is relying on a constant the PDK does
    not stand behind in any documented way, with no independent source to
    validate it against short of silicon. `klt sim` surfaces this
    structurally (never assumes it silently): see "Per-family
    mismatch-activity report" below — a `monte_carlo.vary` request
    including `"mismatch"` reports the resistor family's
    `family_mismatch[].active` as `false`, never lumped in with the active
    MOS/BJT families.

  For untested process corners or PDK variants not listed above, verify that
  the section (sky130-style) or the mismatch switch (gf180mcu-style) resolves in
  the actual `.lib` file before using it in production.

- **`corners.supply_v`** (`object`, optional) — keyed by source/`.param` name
  (`vdd`, `vdda`, …), each an array of volts. **Multiple keys sweep together
  by index** (rails move as a set, not a cross product) — all arrays must be
  the same length. Each value becomes an `alter <key>=<value>` command in the
  generated `.control` block, so `<key>` must name either a voltage source or
  a `.param` the netlist body defines and its sources reference (e.g.
  `.param vdd=1.8` / `Vdd vdd 0 DC {vdd}`).
- **`corners.temperature_c`** (`array<number>`, optional) — degrees Celsius,
  one `.temp <value>` card per point. Defaults to `[27]` when omitted.
- **`exclude`** (`array<object>`, optional) — partial corner specs
  (`process`, `temperature_c`, and/or `supply_v`) dropped from the expansion,
  for sparse matrices.

Expansion is deterministic and odometer-style: process outermost, supply
next, temperature innermost — the same corner list, same order, every run
against the same request.

## Monte Carlo sampling

`request.monte_carlo` (optional) re-runs each expanded corner point `n`
times with a fresh, reproducible random seed, standing in for the
per-instance device variation a mismatch-aware model library's behavioral
parameters (`AGAUSS`/`GAUSS` calls, e.g. sky130's `mc_mm_switch`/
`mc_pr_switch`-gated device parameters) draw on:

```json
{ "monte_carlo": { "n": 300, "seed": 20260801, "vary": "mismatch", "k_sigma": 3 } }
```

Each sample is reported as its own `corners[]` entry (raw, per-sample
values), *and* reduced to a per-measurement statistical verdict under
`measurements[].monte_carlo` — see "Monte Carlo statistics" below.

- **`monte_carlo.n`** (integer, required) — number of samples per expanded
  corner point. Must be a positive integer.
- **`monte_carlo.seed`** (integer, required) — the base seed the entire
  sample sequence derives from (see "Seed contract" below).
- **`monte_carlo.vary`** (string, required) — which axis of variation this
  sample sequence exercises: `"mismatch"`, `"process"`, or `"both"`.
- **`monte_carlo.quantiles`** (array\<number\>, optional) — percentiles in
  `[0, 100]` reported per measurement. Defaults to `[5, 50, 95]` (the
  median plus the symmetric tails). Duplicates are dropped; declaration
  order is preserved and becomes the response key order.
- **`monte_carlo.k_sigma`** (number, optional) — the sigma multiple `k` for
  the run-wide limit-window check (see "Monte Carlo statistics"). Omit for
  no window check. Overridable per measurement with
  `measurements[].k_sigma`.

`monte_carlo` is orthogonal to `corners.*`: the PVT axes still select
*which* process/supply/temperature points are simulated — including an
already-supported mismatch-enabled `.lib` section like sky130's `tt_mm`
(see "Corner axes" above) — while `monte_carlo` asks for each of those
points to be re-run `n` times with a different seed. A request combining
both expands to `corner_count * monte_carlo.n` total runs.

**Seed contract.** `monte_carlo.seed` makes the sampled sequence
reproducible run-to-run: the same `seed` always derives the same per-sample
seed values (module hardware/OS variance aside), in any process, on any
machine — the derivation is SHA-256-based, never Python's salted built-in
`hash()`. Each sample derives two independent components — `process_seed`
and `mismatch_seed` — plus a combined `rndseed` written into the generated
deck as `.options seed=<rndseed>` (ngspice's documented mechanism for
seeding `AGAUSS`/`GAUSS`/`random()`, which must appear before the netlist's
`.lib`/`.include` cards). `mc_process_seed`/`mc_mismatch_seed` are also
exposed as plain `.param`s in the deck, for a netlist body that wants to
reference a per-axis seed directly. `monte_carlo.seed`, `n`, and `vary` are
echoed back in the response's `environment.monte_carlo` (plus `quantiles`
and `k_sigma`, each only when the request declared it); each sample's own
derived values are on that sample's `corners[]` entry, under
`monte_carlo: {sample_index, seed, process_seed, mismatch_seed}` — see
"JSON schema" below.

**Deterministic negative control.** A seed component only varies across
samples when `monte_carlo.vary` actually asks for that axis — requesting
`vary: "process"` derives a different `process_seed` per sample but the
*same* `mismatch_seed` for every sample of a given corner (sigma=0
downstream), and vice versa for `vary: "mismatch"`. This guards against a
broken/no-op sampler that silently produces no variation at all (or
variation from an unrelated source): the axis nobody asked to vary is
provably pinned, not just "happens to look the same" this run. Two public
canary repos (gf180-bandgap, sky130-bandgap) already rely on this exact
guarantee — an MC-off point where every sigma must come back exactly `0` —
in their own hand-rolled orchestration; this is that same guarantee shipped
as first-class `klt sim` behavior.

**Unique sample IDs.** Each sample's `corner_id` and artifact directory
extend the corner it was drawn from with a `/mc<sample_index>` suffix (e.g.
`tt/1.620V/27C/mc0`, `tt/1.620V/27C/mc1`, …) so per-sample logs never
collide under `options.keep_artifacts`, and `exclude`d requests won't be
sampled since they aren't in the corner list. Invalid `monte_carlo` fields
(missing `n`/`seed`/`vary`, a bad `vary` value, `n < 1`) raise `SimError`
before any corner runs — the same "the sweep never started" class as an
unresolvable netlist or unsupported backend.

**Per-family mismatch-activity report.** Turning on the deck's global
mismatch switch/section does not guarantee *every* device family in the
netlist actually gets per-instance variation — see "Known-good mismatch
mechanisms" above: gf180mcu's poly-resistor subcircuits carry the mismatch
hook, but its default is a hardcoded `0` and the vendored deck ships the
Pelgrom-style sigma formula commented out, while MOS and BJT ship that
formula active under the *same* switch. A run whose request, deck, and log
all say "mismatch on" gives no signal that the resistor family was silently
excluded — this is the exact gap issue #355 is about.

When `monte_carlo.vary` is `"mismatch"` or `"both"`, the response's
`environment.monte_carlo` carries an additional `family_mismatch` array:
one entry per distinct device family the netlist instantiates (detected
from plain SPICE element types — `R`/`C`/`D`/`Q`/`M` — and from
recognised keywords in `X` subcircuit-call names, e.g. `nfet_03v3` →
`mosfet`, `res_xpoly_1p1000pl` → `resistor`), each stating whether that
family's mismatch is structurally active in the selected PDK deck:

```json
{
  "family_mismatch": [
    { "family": "mosfet", "active": true, "note": "..." },
    { "family": "resistor", "active": false, "note": "..." },
    { "family": "capacitor", "active": null, "note": "..." }
  ]
}
```

- **`active: true`** — this family's per-instance mismatch is structurally
  live in the selected deck under the request's global mismatch
  switch/section.
- **`active: false`** — this family's mismatch is structurally disabled in
  the selected deck regardless of the global switch (e.g. gf180mcu's poly
  resistor) — a hard-zero-mismatch family is **never** reported as sampled.
- **`active: null`** — not independently verified: either the family has no
  curated entry for the resolved PDK, or `models.pdk` was omitted so the PDK
  itself could not be determined. Treat any spread for a `null` family as
  unconfirmed, not as evidence mismatch was (or wasn't) sampled.

The PDK is resolved from `models.pdk` (e.g. `"gf180mcuC"` → PDK family
`"gf180mcu"`, mirroring the family-prefix resolution `klt extract`'s
device-model binding already uses); the curated table currently covers
gf180mcu (MOS/BJT active, resistor structurally disabled — see above) and
sky130 (MOS/BJT active under an `_mm`-suffixed section). Every other family
and every unrecognised/omitted PDK reports `active: null`. This report is
informational only — `klt sim` does not gate `status` on it, and it is
independent of the per-measurement statistics rollup described in "Monte
Carlo statistics" below: the rollup reduces whatever spread the samples
*did* produce, while this report says whether a given family could have
contributed any spread at all. Read them together — a family reported
`active: false` explains a near-zero `stddev` that the rollup alone would
leave ambiguous. It exists so a caller (or a human) reading the response
can tell a genuinely-sampled family from a structurally-excluded one without
reading the vendored model deck line by line.

## Monte Carlo statistics

A `monte_carlo` request additionally reduces its samples to a **statistical
verdict** per measurement, so a caller never has to re-derive one from the
raw `corners[]` list. The statistics attach to the *existing*
per-measurement rollup (`measurements[]`), as an additive `monte_carlo`
block — the block is present only for a measurement that actually ran under
`monte_carlo`, so a plain corner matrix keeps today's exact response shape:

```json
{
  "name": "vref",
  "unit": "V",
  "limits": { "min": 1.15, "max": 1.25 },
  "status": "pass",
  "worst_case": { "corner_id": "tt/1.800V/27C/mc17", "value": 1.2418, "margin": 0.0082 },
  "monte_carlo": {
    "n": 300,
    "errored": 0,
    "mean": 1.20117,
    "stddev": 0.01342,
    "min": 1.16204,
    "max": 1.24180,
    "quantiles": { "p5": 1.17925, "p50": 1.20106, "p95": 1.22341 },
    "sigma_window": {
      "k": 3.0,
      "low": 1.16091,
      "high": 1.24143,
      "status": "pass",
      "margin": 0.00857
    },
    "by_corner": [
      { "corner_id": "tt/1.800V/27C", "n": 300, "errored": 0, "mean": 1.20117, "...": "..." }
    ]
  }
}
```

| Field           | Type                | Description                                                                                                                          |
| --------------- | ------------------- | ------------------------------------------------------------------------------------------------------------------------------------ |
| `n`             | integer             | Samples that produced a usable number — the population every statistic below is computed over.                                        |
| `errored`       | integer             | Samples whose value was unextractable (`null`) and were therefore excluded. `n + errored` is the total sample count for this measurement. |
| `mean`          | number \| null      | Arithmetic mean. `null` when `n == 0`.                                                                                                |
| `stddev`        | number \| null      | **Sample** standard deviation (Bessel-corrected, `n - 1`) — the estimator for a finite draw from a population. `null` when `n < 2` (undefined, never faked as `0.0`). |
| `min`/`max`     | number \| null      | Extremes of the sample set. `null` when `n == 0`.                                                                                     |
| `quantiles`     | object              | One key per requested percentile, `p<percentile>` (`p5`, `p50`, `p2.5`, …), in `monte_carlo.quantiles` order. Values are linearly interpolated between order statistics (numpy's default / `statistics.quantiles(method="inclusive")`); `p0`/`p100` degenerate to `min`/`max`. Each value is `null` when `n == 0`. |
| `sigma_window`  | object \| null      | The `mean ± k*stddev` limit-window check — `null` unless a `k_sigma` was declared *and* `mean`/`stddev` both exist. See below.        |
| `by_corner`     | array\<object\>     | The same statistics recomputed per originating (pre-sampling) corner, in corner order, each with its own `corner_id`. One entry for the common single-corner request. |

**The limit window.** With a `k_sigma` declared (run-wide on `monte_carlo`,
or per measurement — the per-measurement value wins), `sigma_window` reports
whether `mean ± k*stddev` fits inside that measurement's `limits`:

- `low`/`high` are the window endpoints; `k` echoes the multiple used.
- `status`/`margin` come from the **same** `min`/`max` evaluation and
  `margin` sign convention a single deterministic value goes through: both
  endpoints are scored, the window passes only if both do, and `margin` is
  the worse (smaller) of the two — positive is headroom to the nearest
  binding limit, negative is the worst violation. A measurement with no
  `limits` is reported but never fails (`margin: null`), exactly as for a
  single value.
- A **failing window makes the run fail** (`measurements[].status: "fail"`,
  aggregate `status: "fail"`, exit `3`) even when every individual sample
  passed its limits — that is the whole point of asking the question. This
  is opt-in: without a declared `k_sigma`, nothing about pass/fail changes.
- `k: 0` is legal and degenerates the window to the mean itself.

**Pooled vs. per corner.** The top-level statistics pool every sample of
every corner; `by_corner` splits the same samples by the corner they were
drawn from. For a single-corner Monte Carlo request (the common shape) the
two describe the same draw. For a request that combines a PVT matrix *with*
`monte_carlo`, `by_corner` is the statistically meaningful view — each
corner is its own population, and the pooled block is the union across
populations, which is a useful worst-case envelope but not a distribution
anyone sampled.

## Model library resolution (via `klt pdk`)

`models` resolves through the same discovery/resolution library that backs
`klt pdk` (never a hand-rolled path) — two supported shapes:

```json
{ "models": { "pdk": "sky130A", "lib": "libs.tech/ngspice/sky130.lib.spice" } }
```

Resolved via [`klt pdk find`](pdk.md)'s search order (`--pdk-root`/`$PDK_ROOT`,
the ciel/volare stores, conventional prefixes); `lib`, when relative, is
joined against the resolved variant directory. Optional `models.pdk_root`
pins the search the same way `klt pdk find --pdk-root` does.

```json
{ "models": { "lib": "$PDK_ROOT/sky130A/libs.tech/ngspice/sky130.lib.spice" } }
```

The spike's literal shape, for callers that already resolved `$PDK_ROOT`
themselves (e.g. via `eval "$(klt pdk env)"`). Env vars and `~` are expanded;
a relative path is joined against the request file's directory.

Either way, the resolved library is reported in the response's
`environment.models_lib` as the `{path, scope}` shape (`schema_version` `3`,
issue [#1274](https://github.com/2AMLogic/klayout-tools/issues/1274)) — never
the raw absolute path, which on a normal install sits under the user's home
directory and would leak that layout into every committed report:

```json
{ "environment": { "models_lib": { "path": null, "scope": "external" } } }
```

A library that *does* live inside the invocation's repo reports
`{"path": "<repo-relative>", "scope": "repo"}`; a run with no process axis
(and therefore no resolved library at all) reports
`{"path": null, "scope": "absent"}`. The library is still pinned **by
identity** rather than by location: its SHA-256 is in
`environment.models_lib_sha256` and its name/content hash in
`provenance.deck`, so a stored result can be checked against the exact model
file that produced it.

## Failure classification: from the log, never the exit code

`ngspice -b` reliably exits `0` even when a `.meas` fails or the matrix is
singular (verified empirically against ngspice 46 while building this
command — see the spike's "Failure signalling" survey row). Every corner's
`diagnostics` are classified from its own log file's text:

| `code`             | Detected from                                                              |
| ------------------ | --------------------------------------------------------------------------- |
| `singular_matrix`  | A `Warning: singular matrix` line.                                          |
| `nonconvergence`   | Iteration-limit / gmin-stepping / source-stepping / time-step-too-small text. |
| `netlist`          | A top-level `Error:` line naming a syntax/unknown/undefined/parse/subckt problem. |
| `model_bin_range`  | ngspice's own `Error: could not find a valid modelname` — undiagnostic on its own; enriched with the netlist's likely culprit instance (largest `w`, or `w * nf` when fingered, without `m=`) when one is found. See "Model bin-range diagnostic" below. |
| `timeout`          | The per-corner `options.timeout_s` budget was exceeded; the process is killed. |
| `measurement`      | A declared `.meas` produced no value (missing, not a `"fail"` — see below). |
| `unknown`          | Anything else that prevented a trustworthy result (e.g. ngspice not installed/spawnable). |
| `budget_exceeded`  | The corner never started: the sweep's own `options.wall_clock_budget_s` was exceeded first. See "Wall-clock budget, orphan safety, and resume" above. |
| `orphaned`         | The corner never started: the launching process exited before its turn. See "Wall-clock budget, orphan safety, and resume" above. |
| `lost_shard`       | The corner's `hosts > 1` shard never returned (Epic #375). See "Fleet sharding" above. |
| `batch_job_failed` | The corner never started, or its report was never written: the `batch` backend's fleet job reached a terminal `failed` state with an exit code outside `klt sim`'s own `0`/`3`/`4` (see "Batch backend" above). |
| `batch_job_timeout` | The corner never completed: the `batch` job exceeded its own `timeout_seconds` on the fleet instance and was killed by 2am's harness. |
| `batch_poll_timeout` | The corner's result was never observed: `batch.poll_timeout_s` elapsed with the job still non-terminal. The job may still be running on the fleet — this is what the client knows, not a claim the run failed. |
| `timeout_budget_unreachable` | The corner never started: `options.fail_fast_probe`'s calibration probe measured a rate implying `options.timeout_s` cannot plausibly cover the full analysis window, so the whole grid was aborted before dispatch (issue #1694). Carries additional `reached_s`/`fraction` fields — an *estimate* of how far this corner would have gotten, derived from the measured rate, not a real per-corner recovery. See "Timeout-budget preflight" above. |

**A `diagnostics` entry at `severity: "error"` makes that corner
`status: "error"`**, which always outranks a clean limit violation
(`"fail"`) — `error` means no trustworthy number exists; `fail` means the
simulator produced a trustworthy number and the design missed a limit.
Conflating the two is the specific defect this command exists to avoid (see
the spike's "Semantics and guarantees").

`singular_matrix`/`nonconvergence` are a documented exception, downgraded to
`severity: "warning"` (recorded, but non-fatal) rather than `"error"` when
the run recovered: ngspice's own gmin/source-stepping recovery narrates its
*intermediate* stepping attempts failing (`Warning: singular matrix`,
`... stepping failed`) even on a corner that goes on to complete
successfully — that narration alone is not evidence the analysis failed
(issue #205). The downgrade only applies when every one of the request's
`measurements[]` actually came back with a value for this corner *and*
ngspice's own `simulation(s) aborted` trailer is absent from the log; a
corner with no `measurements[]` declared, a measurement that produced no
value, or that trailer keeps `singular_matrix`/`nonconvergence` at
`severity: "error"` and the corner at `status: "error"`, exactly as before.
`netlist`/`timeout`/`measurement`/`unknown` are never downgraded.

`.meas` cards are also validated against ngspice's own supported analysis
types (`dc`/`ac`/`tran`/`sp`) before a request ever reaches ngspice: ngspice
has no `.MEASURE OP` — an operating point has no sweep variable for a
measurement to search over the way DC/AC/TRAN/SP do — so a `.meas op` card
(regardless of the request's own `analysis.kind`) is rejected up front with
an actionable `SimError` instead of failing deep inside an ngspice parse
error. Use `analysis.kind: "tran"` with a short single-step transient and
`.meas tran ... at=<t>` to read back an operating-point-like value instead.

### Model bin-range diagnostic (`model_bin_range`)

Some PDKs' BSIM model cards enforce an undocumented per-instance width *bin
range* — a device sized past it fails ngspice with a generic
`Error: could not find a valid modelname`, which says nothing about width
and reads like the device name is wrong or the model library never loaded.
gf180mcu's `nfet_06v0`/`pfet_06v0` hit this at roughly 100–110 µm of total
instance width, and the failure is identical whether the width comes from a
single large `w=` or from fingering it via `nf=` — `nf=` parallels *inside*
the same model-card instance the width check applies to, so it trips the
same ceiling `w=` alone does. Only `m=` (the parallel-*instance* multiplier,
applied outside that check, with each instance's own `w=` kept under the
ceiling) reliably works around it (issue #1214).

When ngspice's log matches this failure, `klt sim` re-scans the corner's own
netlist for MOS-shaped instances (`M`/`X` cards) and picks out the one with
the largest total width (`w`, or `w * nf` when fingered) that does not
already use `m=` — the most likely culprit — and reports it by name in the
`model_bin_range` diagnostic's `message`, e.g.:

```
ngspice reported "could not find a valid modelname" -- likely cause:
instance 'X5' (nfet_06v0) with w=50u nf=450 (22500 um effective width)
exceeds the model card's per-instance width bin range (undocumented,
~100 um on gf180mcu); use m= to parallel multiple smaller-width instances
instead of a single large w= or nf= (see issue #1214)
```

This is a heuristic over the netlist text, not a validated PDK limit: the
100 µm figure is an empirical floor observed on gf180mcu, not a value this
tool derives from the model file itself (the root cause — the PDK's model
cards not documenting their own bin ranges — is upstream, not something this
repo can fix). When no such instance is found, `message` falls back to
ngspice's raw log line, same as every other `code`.

## Waveform artifact (optional, first-class)

When `options.waveforms` is true, each corner's `.control` block adds
`set filetype=ascii` and `write <rawfile>` before running the declared
analysis; the resulting ngspice ASCII rawfile is parsed into a small,
documented waveform JSON shape and referenced (not inlined) from the
corner's `artifacts.waveform`:

```json
{
  "plotname": "Transient Analysis",
  "variables": [
    { "index": 0, "name": "time", "type": "time" },
    { "index": 1, "name": "v(out)", "type": "voltage" }
  ],
  "points": [[0.0, 0.81], [1e-11, 0.81], ...]
}
```

`variables[0]` is always the sweep variable (`time` for `.tran`, frequency
for `.ac`, the swept source for `.dc`); each `points[]` entry has one value
per declared variable, in `variables[].index` order. Waveform data is never
inlined into the response — only `artifacts.raw` (the rawfile itself) and
`artifacts.waveform` (its parsed JSON) paths are.

### Complex-valued (`ac`/`sp`) rawfiles

An `ac`/`sp` rawfile declares `Flags: complex` in its header, and ngspice
writes every value in the `Values:` section as a comma-separated `real,imag`
pair rather than a bare float. `points[]` always holds plain real-valued
floats (per the JSON contract's additive-only convention — the shape never
changes to `[real, imag]` tuples), so for a `Flags: complex` rawfile each
column's value is reduced to its **magnitude** (`hypot(real, imag)`) before
being written into `points[]`. This is applied uniformly to every column,
including the sweep variable (frequency): its imaginary part is always `0`
in practice, so its magnitude and real part are the same value. `tran`/`dc`
rawfiles (`Flags: real`) are unaffected and continue to report bare floats.

## Waveform plots (`--plot <dir>`, issue #1723)

`--plot <dir>` renders one self-contained, dependency-free SVG per
non-sweep signal per corner to `<dir>` — the same no-plotting-library,
string-built SVG approach `klt trajectory --plot` uses for its
objective-vs-turn plot, applied to a `tran`/`ac`/`dc` waveform instead. It is
the *visual* half of diagnosing a measurement miss (a stuck comparator, a
saturating integrator, a ring oscillator that never starts) — issue #56
covers the *numeric* half (threshold-crossing extraction) separately.

`--plot` forces `options.waveforms`/`options.keep_artifacts` on for the
run, regardless of the request's own settings — a waveform can only be
plotted once captured and persisted to disk. Filenames are deterministic:
`<plot_dir>/<corner_slug>__<signal_slug>.svg`, where `<corner_slug>` mirrors
a corner's own artifact-directory slug (e.g. `ss/1.620V/125C` →
`ss_1p620V_125C`) and `<signal_slug>` is the waveform variable name with
non-alphanumeric characters collapsed to `_` (e.g. `v(out)` → `v_out`).

Every written file is listed in the response's top-level `plots` field and
in each corner's own `artifacts.plots`:

```json
{
  "plots": [
    { "corner_id": "tt/1.800V/27C", "signal": "v(out)", "path": "/abs/plots/tt_1p800V_27C__v_out.svg" }
  ]
}
```

`ac` plots use a log10-scaled sweep (frequency) axis; `tran`/`dc` use a
linear one. On a `tran` analysis, a measurement whose `.meas` card's own
signal matches a rendered signal additionally gets that measurement's
`from=`/`to=` extraction window shaded on the plot, and its `WHEN
<signal>=<value>` threshold drawn as a dashed reference line — the two
pieces of visual context a flat/stuck signal's plot needs to explain why it
missed.

**On a measurement miss, the miss report names the plot.** Every entry in
the top-level `measurements[]` rollup gets a `plot` field (a path, or
`null` when no rendered plot matches that measurement's own signal) —
present only when `--plot` was used, pointing at the SVG for that
measurement's own `worst_case` corner:

```json
{ "name": "startup_time", "status": "error", "worst_case": { "corner_id": "tt/1.800V/27C", "value": null, "margin": null }, "plot": "/abs/plots/tt_1p800V_27C__v_out.svg" }
```

so an agent (or a skill, e.g. `design-sizing`'s Loop A) reading a miss knows
exactly which picture to open next, without re-deriving which signal a
`.meas` card was even about.

## Post-layout verification (`netlist_source`)

`request.netlist_source` (optional, `"schematic"` | `"extracted"`) lets a
caller declare which side of the design loop a given `netlist` represents. It
is a plain data field — `klt sim` never tries to infer provenance from the
netlist file itself (a schematic-level netlist and one extracted from a
laid-out design can be syntactically indistinguishable), so the caller states
it explicitly, the same pattern already used for `engine`/`backend`. When
provided, it is echoed back verbatim in the response's
`environment.netlist_source`; when omitted, `environment` has no such key and
every other field is unaffected — this is an additive, backward-compatible
change.

The intended use is the post-layout re-verification pass: after Loop B
converges (DRC/LVS clean), run `klt extract` on the layout and re-run the
*same* testbench/corner-matrix request — same stimuli, same measurement
limits — against the extracted netlist, this time with
`netlist_source: "extracted"`. Comparing that run's `environment` block
against the earlier `netlist_source: "schematic"` run (or a request that
omitted the field, treated as pre-layout by convention) is how a caller
distinguishes "simulation-verified pre-layout" from "simulation-verified
post-layout" results for the same design. `klt sim` applies no extra
pass/fail gating based on this field — a corner matrix run with
`netlist_source: "extracted"` passes or fails on exactly the same per-corner
measurement limits as any other run. For the full workflow (why this matters,
how it fits the extract→sim handoff, and what it does and does not prove
before RC-parasitic extraction lands) see
[`.claude/skills/design-extraction/SKILL.md`](../../.claude/skills/design-extraction/SKILL.md)
and [`docs/design/design-pipeline.md`](../design/design-pipeline.md)'s S9/S10
stages.

## Evidence discipline: records, supersession, and pinning are repo-owned

`klt sim` is a stateless, single-invocation command: `provenance` and
`environment` pin the inputs a *given* run used (netlist hash, model-library
hash, PDK version, engine version) but nothing persists across runs, chains
one run to the next, or enforces a policy from a previous run's result. That
scope decision — and how a caller should build append-only evidence records,
supersession chains, PDK-pin enforcement, subset-reason requirements, and
cross-corner spread checks on top of the existing per-corner data — is
decided in
[`docs/design/sim-evidence-discipline-spike.md`](../design/sim-evidence-discipline-spike.md).
Short version: four of those five are entirely a documented repo-level
convention built from fields `klt sim` already emits (the same
report-not-judge precedent `measurements[].limits` already sets); one — a
per-corner netlist snapshot — is a genuine contract gap tracked by
[#356](https://github.com/2AMLogic/klayout-tools/issues/356).

## Operating-point lint (`--op-lint`)

`klt sim --op-lint <request.json>` answers a different, much cheaper
question than the corner sweep: **"which MOSFET in this netlist is not doing
its job, and why?"** It runs a single `.op` analysis at one corner, walks
every MOS instance in the netlist, and reports per-device findings — the
first thing to run when a measurement misses, *before* spending another Loop
A sizing pass (issue #1718).

It takes the **same request document** the sweep takes — only `netlist` is
required; `models`/`corners` are read exactly as documented above;
`analysis`/`measurements` are not needed for the analysis itself (this mode
always runs a plain `op`). A `--op-lint` run never runs the corner matrix,
never writes artifacts, and emits its own envelope (below) instead of the
sweep response.

```
klt sim <request.json> --op-lint [--op-lint-corner <corner_id>] [--format text|json]
```

- `--op-lint-corner` — which expanded `corner_id` from the request's own
  matrix to bias at. Defaults to the **first** expanded corner. Exactly one
  corner is run either way: this is a diagnostic meant to be cheap enough to
  run on every miss, not a second sweep.

### What it checks

| `check` | Severity | Condition |
| --- | --- | --- |
| `off` | `error` | `\|Vgs\| < \|Vth\|` **and** `\|Id\|` at or below `thresholds.off_current_a` (default `1e-9` A). A device conducting real current below `Vth` is weak inversion — a legitimate design point — and is reported as `region: "subthreshold"` with no finding. |
| `triode` | `warning` | `\|Vds\| < \|Vdsat\| + margin`, where the margin is **corner-aware**: `thresholds.triode_margin_kt` (default `2`) thermal voltages `kT/q` at *this corner's* temperature — ~40 mV at −40 °C, ~52 mV at 27 °C, ~69 mV at 125 °C. `thresholds.triode_margin_v` pins it absolutely instead. |
| `drain_tied_to_rail` | `error` | An NMOS whose drain node is ground, or a PMOS whose drain node is the positive supply — the drain voltage can never move. |
| `gate_shorted_to_source` | `error` | Gate and source are the same node: `Vgs` is 0 by construction. |
| `bulk_not_tied` | `warning` | Bulk is tied to neither the device's own source nor the matching rail. A warning, not an error — a deep-nwell/isolated device may genuinely want this. |
| `missing_node` | `error` | A declared I/O node (`op_lint.nodes`, or any `v(<node>)` reference inside the request's own `.meas` cards) that does not appear in the netlist at all — the usual cause of a measurement that silently reads 0 V. |
| `floating_node` | `warning` | A node with exactly one element terminal on it and no DC path. This is the *structural* counterpart of the singular-matrix / gmin-stepping narration the sweep classifies from the log ([#205](https://github.com/2AMLogic/klayout-tools/issues/205)); that classification is reused here verbatim rather than re-implemented, so the two never disagree. |

Every threshold comparison is on **magnitudes**: BSIM4 reports a PMOS's
`vgs`/`vds`/`vth`/`vdsat` as positive numbers while a bare SPICE level-1
device reports `vdsat` negative for the same device, so only `abs()`
comparisons classify both consistently.

`Vth` is always read **from the model** (`@m...[vth]`), per device — never a
constant — which is why a device's reported `vth_v` differs between channel
types and between W/L bins on the same PDK.

Rails are identified from the request, not guessed: the keys of
`corners.supply_v` name the source elements `klt sim` `alter`s, so each
one's positive terminal is a declared supply rail; ground is `0` plus the
conventional spellings (`gnd`, `vss`, …) and anything a 0 V source ties to
them. A testbench's own stimulus source (`Vin in 0 DC 0.9`) is deliberately
*not* promoted to a rail. `op_lint.rails.supply`/`.ground` override both.

The **structural** checks (`drain_tied_to_rail`, `gate_shorted_to_source`,
`bulk_not_tied`, `missing_node`, `floating_node`) need no simulator, so they
are still reported when the operating point itself fails to converge — which
is exactly when an agent most needs them.

### Optional `op_lint` request block

```json
"op_lint": {
  "thresholds": { "off_current_a": 1e-9, "triode_margin_kt": 2.0 },
  "nodes": { "inputs": ["inp", "inn"], "outputs": ["out"] },
  "rails": { "supply": ["vdd"], "ground": ["0", "vgnd"] },
  "models": {
    "nfet_03v3": { "kind": "nmos", "op_point_element": "m0" }
  },
  "timeout_s": 120
}
```

Every key is optional. `models.<model>.op_point_element` names the *inner*
element of a PDK device subcircuit, which is a per-PDK convention rather
than a standard: sky130 names it `m` + the subcircuit name
(`msky130_fd_pr__nfet_01v8`), gf180mcu names it `m0`. Both are probed
automatically — the generated deck `print`s every candidate path and an
unknown vector name is a harmless log line in ngspice batch mode — so this
override is only needed for a PDK following neither convention. When no
candidate answers for a device, a `op_point_unavailable` diagnostic names
the device and the paths that were tried instead of silently reporting
nothing. `models.<model>.kind` likewise resolves a model name this tool
cannot classify as n- or p-channel.

### Response

```json
{
  "schema_version": 1,
  "status": "findings",
  "netlist": { "path": "examples/design-pipeline/ota_5t.spice", "scope": "repo" },
  "corner": { "corner_id": "ss/1.620V/125C", "process": "ss", "supply_v": { "vdd": 1.62 }, "temperature_c": 125 },
  "device_count": 6,
  "finding_count": 1,
  "error_count": 0,
  "warning_count": 1,
  "thresholds": { "triode_margin_v": 0.06862, "off_current_a": 1e-9 },
  "rails": { "ground": ["0"], "supply": ["vdd"] },
  "devices": [
    {
      "name": "XM5",
      "kind": "nmos",
      "model": "sky130_fd_pr__nfet_01v8",
      "op_point_element": "m.xm5.msky130_fd_pr__nfet_01v8",
      "nodes": { "drain": "tail", "gate": "nbias", "source": "0", "bulk": "0" },
      "values": { "vgs_v": 0.72, "vth_v": 0.64, "vds_v": 0.166, "vdsat_v": 0.097, "id_a": 1.83e-5 },
      "region": "triode"
    }
  ],
  "findings": [
    {
      "check": "triode",
      "severity": "warning",
      "device": "XM5",
      "kind": "nmos",
      "node": null,
      "nodes": { "drain": "tail", "gate": "nbias", "source": "0", "bulk": "0" },
      "values": { "vgs_v": 0.72, "vth_v": 0.64, "vds_v": 0.166, "vdsat_v": 0.097, "id_a": 1.83e-5 },
      "message": "XM5 (nmos): |Vds| 0.166 V is below |Vdsat| 0.097 V + 69 mV margin -- the device is in triode, not saturation",
      "suggestion": "give node `tail` more headroom above `0` ..."
    }
  ],
  "node_voltages": { "out": 0.892, "tail": 0.166, "vdd": 1.62 },
  "diagnostics": [],
  "environment": { "engine": "ngspice", "engine_version": "46", "models_lib": { "path": null, "scope": "external" }, "models_lib_sha256": "…", "netlist_sha256": "…", "runtime_s": 48.05 },
  "provenance": { "…": "the shared block (docs/json-contract.md)" }
}
```

| Field | Meaning |
| --- | --- |
| `status` | `"clean"` (no findings), `"findings"` (at least one, of any severity), or `"error"` (the operating point itself produced no trustworthy result). |
| `finding_count` / `error_count` / `warning_count` | Totals. `error_count` is what the exit code and the `klt eval` gate key off; `finding_count` is the headline number. |
| `devices[]` | Every MOS instance found at the netlist body's top level, in netlist order, with its four terminal nodes, model-reported values (`null` for any the model does not expose), the resolved `op_point_element`, and its `region` (`off`/`subthreshold`/`triode`/`saturation`/`unknown`). |
| `findings[]` | One entry per finding, in device-then-node order. `device`/`kind`/`nodes`/`values` are `null` on a node-level finding and `node` is `null` on a device-level one — every key is always present. |
| `node_voltages` | Every probed node's DC voltage, for the agent's own reasoning. |
| `diagnostics[]` | The same `{severity, code, message}` shape the sweep's per-corner diagnostics use, plus this mode's own `op_point_unavailable` / `device_kind_unknown` codes. |
| `thresholds` | The thresholds this run actually applied, including the corner-derived `triode_margin_v` — so a finding is reproducible without re-deriving the margin. |

Exit codes keep the same meaning as the sweep's: `0` ran with no
`error`-severity finding (warnings alone do **not** fail), `1` could not run
at all, `3` at least one `error`-severity finding, `4` `status: "error"`.

The same entry point is available as a `klt eval` gate kind, `op-sanity` —
see [`docs/cli/eval.md`](eval.md).

## JSON schema (the contract)

**JSON is the API.** Human-readable text output is a courtesy; the schema
below is the stable contract, subject to the same rules as every other `klt`
verb — see [`docs/json-contract.md`](../json-contract.md) for the envelope
(`schema_version`, error shape, exit codes) shared across all commands.

**The response's `netlist`, `environment.resume.checkpoint_path`, and
`environment.models_lib` report `{path, scope}`, never a raw path string**
(`schema_version` `2` for the first two, issue #1261; `schema_version` `3`
added `environment.models_lib`, issue #1274). All three are normalised via
`env_provenance.repo_relative_path` — the same primitive and shape
[`env-provenance.md`](env-provenance.md) documents: `scope: "repo"` with a
repo-relative `path` when the input sits inside the invocation's repo, else
`{"path": null, "scope": "external"}` (or `{"path": null, "scope":
"absent"}` when nothing was resolved) — the absolute path is never echoed. A
committed evidence record wraps a `klt sim` response unmodified
(`docs/design/sim-evidence-discipline-spike.md`), so an absolute path in any
of these fields used to leak the run's own filesystem layout (author's home
directory, a Loom worktree number) into every such record. The **request**'s
own `netlist`/`models` fields (below) are unaffected — this only changes what
the *response* echoes back.

### Request

```json
{
  "netlist": "testbench.spice",
  "engine": "ngspice",
  "models": { "lib": "corner.lib" },
  "corners": {
    "process": ["tt", "ss"],
    "supply_v": { "vdd": [1.62, 1.98] },
    "temperature_c": [-40, 125]
  },
  "exclude": [{ "process": "ss", "temperature_c": -40 }],
  "analysis": { "kind": "tran", "args": "1n 5u" },
  "measurements": [
    {
      "name": "vout",
      "spice": ".meas tran vout FIND v(out) AT=5u",
      "unit": "V",
      "limits": { "min": 0.75, "max": 1.05 }
    }
  ],
  "options": { "timeout_s": 30, "keep_artifacts": false, "waveforms": false }
}
```

| Field                    | Type              | Description                                                                                                                                                             |
| ------------------------ | ----------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `netlist`                | string, required  | Path to the circuit-body netlist under test (see "Netlist convention" above). Relative paths resolve against the request file's directory.                            |
| `engine`                 | string            | Engine selector. Defaults to, and currently only supports, `"ngspice"`.                                                                                                |
| `backend`                | string            | Execution backend for the corner matrix. Defaults to `"local"` (runs corners sequentially in-process); `"local-parallel"` runs the same matrix across a bounded local worker pool; `"remote"` provisions an EC2 instance and runs it there; `"batch"` submits an S3 job contract to 2am's EDA batch fleet (see "Execution backends", "Remote backend", and "Batch backend" above). Overridable with the `--backend` CLI flag. |
| `remote.*`               | object            | Request fields for the `remote` backend (`region`, `key_name`, `ssh_key_path`, `launcher_cidr`/`launcher_cidrs`/`security_group_id`, `subnet_id`, `ssh_user`, `provider`, `spot`, `max_hourly_cost_usd`, `ssh_ready_timeout_s`, `ssh_timeout_s`, `ami_manifest`) — see "Remote backend" above. Only read/validated when `backend: "remote"` is selected. |
| `batch.*`                | object            | Request fields for the `batch` backend (`provision_script_path`, `bucket`, `jobs_prefix`, `region`, `profile`, `poll_interval_s`, `poll_timeout_s`) — see "Batch backend" above. Only read/validated when `backend: "batch"` is selected. |
| `remote.hosts`           | integer           | Shard the expanded unit list across this many hosts and merge the per-shard reports. Defaults to `1` (today's single-host behaviour, byte-identical). Must be a positive integer, and (for `backend: "remote"`) no greater than the unit count. `local`/`local-parallel` shard in-process; `backend: "remote"` provisions a real `hosts`-instance EC2 fleet ([#906](https://github.com/2AMLogic/klayout-tools/issues/906)). Overridable with the `--hosts` CLI flag, same precedence rule as `backend`/`--backend`. See "Fleet sharding" above. |
| `models.lib`             | string            | Model library to bind process-corner `.lib` sections from. Required only when `corners.process` is set. See "Model library resolution" above.                        |
| `models.pdk`/`pdk_root`  | string            | Resolve `models.lib` through `klt pdk find` instead of a literal path.                                                                                                 |
| `corners.process`        | array\<string \| {name, sections}\> | Process-corner axis. Each entry is either a bare `.lib` section name (single `.lib` card) or a bundle object `{"name": str, "sections": list[str]}` (one `.lib` card per section, in order) — see "Corner axes" above. |
| `corners.supply_v`       | object            | Supply axis, keyed by source/`.param` name; arrays sweep together by index.                                                                                            |
| `corners.temperature_c`  | array\<number\>   | Temperature axis, degrees Celsius. Defaults to `[27]`.                                                                                                                 |
| `exclude`                | array\<object\>   | Partial corner specs dropped from the expansion.                                                                                                                       |
| `monte_carlo.n`          | integer           | Number of samples per expanded corner point. Positive integer, required when `monte_carlo` is present. See "Monte Carlo sampling" above.                              |
| `monte_carlo.seed`       | integer           | Base seed the sample sequence derives from. Required when `monte_carlo` is present.                                                                                    |
| `monte_carlo.vary`       | string            | `"mismatch"`, `"process"`, or `"both"` — which axis the sample sequence varies. Required when `monte_carlo` is present.                                                 |
| `monte_carlo.quantiles`  | array\<number\>   | Percentiles in `[0, 100]` reported per measurement. Defaults to `[5, 50, 95]`. See "Monte Carlo statistics" above.                                                      |
| `monte_carlo.k_sigma`    | number            | Run-wide sigma multiple `k` for the `mean ± k*stddev` limit-window check. Omit for no window check. Must be a non-negative number.                                      |
| `analysis`               | object, required  | `kind` (e.g. `"op"`, `"dc"`, `"ac"`, `"tran"`) and `args`, the engine-syntax analysis-card arguments. One analysis per request. `"op"` is a valid `kind`, but see `measurements[]` below — it cannot be paired with a `.meas op` card. |
| `measurements[]`         | array\<object\>   | `name` (stable response key) and `spice` (a verbatim `.meas` card), plus optional `unit`, `limits` (`min`/`max`, either optional), and `k_sigma` (per-measurement override of `monte_carlo.k_sigma`). No `limits` -> reported, never fails. `spice`'s declared analysis type must be one ngspice's own `.MEASURE` implements (`dc`/`ac`/`tran`/`sp`) — there is no `.MEASURE OP`; a `.meas op` card is rejected up front (`SimError`), regardless of the request's own `analysis.kind`. |
| `options.timeout_s`      | number            | Per-corner wall-clock budget. Defaults to `120`. Exceeding it kills the process and yields an `error`-status corner.                                                    |
| `options.keep_artifacts` | boolean           | Retain per-corner logs/rawfiles on disk under `--outdir` (or its default) and reference them from the response. Defaults to `false`.                                   |
| `options.waveforms`      | boolean           | Capture the optional waveform artifact (see above). Defaults to `false`.                                                                                               |
| `options.max_workers`    | integer           | Worker-pool size for the `local-parallel` backend; ignored by `local`. Must be a positive integer. Defaults to a conservative estimate derived from the local CPU count (see "Execution backends" above). Overridable with the `--max-workers` CLI flag. |
| `options.wall_clock_budget_s` | number       | Overall wall-clock budget in seconds for the whole sweep. Must be a positive number. Defaults to unbounded (today's behaviour). Overridable with the `--budget-s` CLI flag. See "Wall-clock budget, orphan safety, and resume" above. |
| `options.resume`         | boolean           | Resume from a matching on-disk checkpoint under `--outdir`, skipping corners already completed by a prior interrupted run of this same request. Defaults to `false`. Overridable with the `--resume` CLI flag. Not supported with `backend: "remote"` (application error, exit 1). See "Wall-clock budget, orphan safety, and resume" above. |
| `options.fail_fast_probe` | boolean          | Two-pass fail-fast probe (issue #1694): run a bounded calibration `tran` slice on the grid's first corner before dispatching any real corner, and abort the whole grid if the measured rate implies `options.timeout_s` cannot plausibly cover the full analysis window. Defaults to `false`. Overridable with the `--fail-fast-probe` CLI flag. Only applies to `kind: "tran"` analyses on the `local`/`local-parallel` backends. See "Timeout-budget preflight" above. |
| *(CLI-only)* `--plot <dir>` | string         | No request-document equivalent (like `trajectory --plot`) — writes one waveform SVG per non-sweep signal per corner to `<dir>`, forcing `options.waveforms`/`keep_artifacts` on for this run. See "Waveform plots" above. |
| `netlist_source`         | string            | Optional caller-declared provenance of `netlist`: `"schematic"` (pre-layout, e.g. an S6 sizing netlist) or `"extracted"` (post-layout, from `klt extract`). Omit for unchanged behavior — the field is purely additive. An unrecognized value is an application error (exit 1). See "Post-layout verification" below. |

### Response

```json
{
  "schema_version": 3,
  "netlist": { "path": "testbench.spice", "scope": "repo" },
  "status": "pass",
  "corner_count": 8,
  "passed": 8,
  "failed": 0,
  "errored": 0,
  "metrics": {
    "sim__corner__count": 8,
    "sim__corner__passed_count": 8,
    "sim__corner__failed_count": 0,
    "sim__corner__errored_count": 0
  },
  "environment": {
    "engine": "ngspice",
    "engine_version": "46",
    "models_lib": { "path": null, "scope": "external" },
    "models_lib_sha256": "3ccce27a...",
    "netlist_sha256": "71d273ab...",
    "netlist_source": "extracted",
    "monte_carlo": {
      "n": 300,
      "seed": 20260801,
      "vary": "mismatch",
      "family_mismatch": [
        { "family": "mosfet", "active": true, "note": "..." },
        { "family": "resistor", "active": false, "note": "..." }
      ]
    }
  },
  "provenance": {
    "klt_version": "0.4.2",
    "klayout_version": "0.30.10",
    "pdk": { "name": "sky130A", "source": "volare", "version": "<stamp>" },
    "deck": { "name": "corner.lib", "content_hash": "sha256:<hex>" }
  },
  "measurements": [
    {
      "name": "vout",
      "unit": "V",
      "limits": { "min": 0.75, "max": 1.05 },
      "status": "pass",
      "worst_case": { "corner_id": "ss/1.620V/-40C", "value": 0.753488, "margin": 0.003488 }
    }
  ],
  "corners": [
    {
      "corner_id": "tt/1.620V/-40C",
      "process": "tt",
      "supply_v": { "vdd": 1.62 },
      "temperature_c": -40,
      "status": "pass",
      "runtime_s": 0.082,
      "measurements": [
        { "name": "vout", "value": 0.81, "unit": "V", "status": "pass", "margin": 0.06 }
      ],
      "diagnostics": [],
      "artifacts": { "log": null, "raw": null, "waveform": null, "deck": null },
      "monte_carlo": null
    }
  ]
}
```

A sample corner (only present when the request declares `monte_carlo`)
carries a non-null `monte_carlo` block and a `/mc<sample_index>`-suffixed
`corner_id`:

```json
{
  "corner_id": "tt/1.620V/-40C/mc0",
  "process": "tt",
  "supply_v": { "vdd": 1.62 },
  "temperature_c": -40,
  "status": "pass",
  "runtime_s": 0.079,
  "measurements": [ /* ... */ ],
  "diagnostics": [],
  "artifacts": { "log": null, "raw": null, "waveform": null, "deck": null },
  "monte_carlo": {
    "sample_index": 0,
    "seed": 1732958821,
    "process_seed": 90210,
    "mismatch_seed": 552013
  }
}
```

#### Top-level fields

| Field           | Type            | Description                                                                                                   |
| --------------- | --------------- | --------------------------------------------------------------------------------------------------------------- |
| `schema_version`| integer         | Version of this command's JSON shape (`3` as of issue #1274, which extended issue #1261's `{path, scope}` path-normalization to `environment.models_lib`; per-command, per `docs/json-contract.md`).                 |
| `netlist`       | object          | `{path, scope}` — the resolved `netlist` path, normalised via `env_provenance.repo_relative_path` (issue #1261): `scope: "repo"` with a repo-relative `path` when it sits inside the invocation's repo, else `{"path": null, "scope": "external"}`. The absolute path is never echoed. |
| `status`        | string          | Aggregate: `"pass"`, `"fail"`, or `"error"`. Precedence: `error` > `fail` > `pass`.                              |
| `corner_count`  | integer         | Number of entries in `corners` after expansion and `exclude` — always `== len(corners)`.                        |
| `passed`/`failed`/`errored` | integer | Corner counts by status.                                                                                  |
| `metrics`       | object          | Declared-namespace re-keying of `corner_count`/`passed`/`failed`/`errored` (issue #1849). See below. |
| `coverage`      | object          | What this `status` was actually graded over (issue #1996) — always present, purely additive. See "`coverage`" below. |
| `environment`   | object          | Reproducibility block: engine name/version, `models_lib` (the resolved model library as `{path, scope}`, issue #1274 — `{"path": null, "scope": "external"}` for the usual out-of-repo PDK, `"absent"` when no process axis made one necessary; never an absolute path) + its SHA-256, netlist SHA-256, and (when the request declares them) `netlist_source`/`monte_carlo` (`{n, seed, vary}` echoed from the request, plus `quantiles`/`k_sigma` when declared and `family_mismatch` when `vary` includes `"mismatch"` — see "Monte Carlo sampling" above), `budget` (when `options.wall_clock_budget_s` was declared), `orphaned: true` (only when the always-on parent-death check actually fired), and `resume` (when `options.resume` was requested — `resume.checkpoint_path` is the same `{path, scope}` shape as `netlist`, issue #1261) — see "Wall-clock budget, orphan safety, and resume" above. Also carries `timeout_preflight_warning` (string, issue #1686) when the coarse pre-grid `options.timeout_s` sanity check has something to say about a `tran` analysis's declared step/window — advisory only, never blocks the sweep, and absent for the common case — and `fail_fast_probe` (object, issue #1694) when `options.fail_fast_probe`/`--fail-fast-probe` opted in and the calibration probe ran and came back conclusive (present whether or not it aborted the grid); see "Timeout-budget preflight" above for both fields' shapes. |
| `provenance`    | object          | Shared reproducibility block (`klt_version`, `klayout_version`, `pdk`, `deck`) defined once in [`docs/json-contract.md`](../json-contract.md). `pdk` is best-effort from `models.pdk` (else `null`); `deck` pins the resolved model library (`name` = its filename, `content_hash` = `sha256:` digest) when a process axis resolved one, else `null`. Complements the sim-specific `environment` block, which hashes the same library alongside the netlist. |
| `measurements`  | array\<object\> | Per-measurement rollup across all corners: `name`, `unit`, `limits`, aggregate `status`, and `worst_case` (the worst corner and its margin). A measurement that ran under `monte_carlo` additionally carries a `monte_carlo` statistics block (`{n, errored, mean, stddev, min, max, quantiles, sigma_window, by_corner}`) — see "Monte Carlo statistics" above. Additive/optional (issue #1723): only present when `--plot` was used, each entry also carries `plot` — the SVG path for that measurement's own signal at its `worst_case` corner, or `null` if no rendered plot matches. See "Waveform plots" above. |
| `corners`       | array\<object\> | One entry per expanded corner, always `corner_count` entries, in the deterministic expansion order.             |
| `plots`         | array\<object\> | Additive/optional (issue #1723): only present when `--plot` was used — every SVG actually written, as `{corner_id, signal, path}`, in corner/signal order. See "Waveform plots" above. |

#### `metrics` object

Introduced by issue #1849, adopting `klayout_tools.metrics`'s declared
metric namespace registry (issue #247 — see
[`../design/metric-namespace.md`](../design/metric-namespace.md) and
[`../json-contract.md`](../json-contract.md)'s "Declared metric namespace"
section) into `klt sim`'s own top-level payload, following `klt drc`'s own
adoption (issue #1847). **Purely additive**: a re-keying of
`corner_count`/`passed`/`failed`/`errored` under their declared,
METRICS2.1-style hierarchical names — it never replaces or changes those
fields, which stay exactly as documented above. Always present.

| Key                            | Source field   | Description                                                                                                    |
| ------------------------------- | -------------- | ------------------------------------------------------------------------------------------------------------------ |
| `sim__corner__count`            | `corner_count` | Total number of corners in the sweep; `aggregator: "sum"`, no declared quality polarity in the registry.       |
| `sim__corner__passed_count`     | `passed`       | Number of corners that passed; `aggregator: "sum"`, `higher_is_better: true` in the registry.                  |
| `sim__corner__failed_count`     | `failed`       | Number of corners that failed; `aggregator: "sum"`, `higher_is_better: false`, `critical: true` in the registry. |
| `sim__corner__errored_count`    | `errored`      | Number of corners that errored; `aggregator: "sum"`, `higher_is_better: false`, `critical: true` in the registry. |

**`measurements[].name` stays permanently out of scope for this registry.**
Each measurement's `name` (e.g. `"vout"`, `"gain_db"`) is defined by the
*caller's own request spec* above, not by `klt` — `klt` has no way to
declare ahead of time what a caller will choose to measure, so there is no
`measurements__*`-style declared name for an individual measurement's value
or status. A future corner-qualified convention for declaring the *shape*
of a caller-supplied measurement (e.g. METRICS2.1's
`timing__setup__ws__corner:ss`-style corner qualifier) remains an open
design question for a future issue, not resolved here — only the four fixed
corner-sweep rollup fields above are declared.

#### `corners[]` entries

| Field            | Type             | Description                                                                                                                   |
| ---------------- | ---------------- | ----------------------------------------------------------------------------------------------------------------------------- |
| `corner_id`      | string           | `<process>/<supply>V/<temp>C` (e.g. `ss/1.620V/125C`); `<process>` is `"default"` and `<supply>` is `"novdd"` when that axis is absent from the request. Multiple supply rails join as `key=value` pairs. A Monte Carlo sample appends `/mc<sample_index>` (e.g. `ss/1.620V/125C/mc7`). |
| `process`        | string \| null   | Process-corner section used, or `null` when the request declares no process axis.                                              |
| `supply_v`       | object           | Supply values for this corner, keyed by source/`.param` name (`{}` when the request declares no supply axis).                  |
| `temperature_c`  | number           | Temperature for this corner.                                                                                                  |
| `status`         | string           | `"pass"`, `"fail"`, or `"error"`.                                                                                              |
| `runtime_s`      | number           | Engine wall-clock time for this corner (or time-to-timeout, on a killed run).                                                  |
| `measurements[]` | array\<object\>  | `name`, `value` (number, or `null` when unextractable), `unit`, `status` (`"pass"`/`"fail"`/`"error"`), `margin`.               |
| `diagnostics`    | array\<object\>  | `{ "severity": "error"\|"warning", "code": "...", "message": "..." }` — see the classification table above. `"warning"` only occurs for a recovered `singular_matrix`/`nonconvergence` (does not affect `status`); every other code is always `"error"`. Empty for a clean run.       |
| `artifacts`      | object           | `{"log": ..., "raw": ..., "waveform": ..., "deck": ...}`, each an absolute path or `null`. All `null` unless `options.keep_artifacts` is true; `raw`/`waveform` additionally require `options.waveforms`. `deck` is the exact per-corner ngspice deck synthesized for this corner (`.lib`/`.temp`/`alter` lines included) -- the file ngspice actually consumed, not a hash of the unexpanded source netlist (see `environment.netlist_sha256` for that). Raw log text is **never** inlined into the JSON. Additive/optional (issue #1723): also carries `plots` (array of `{signal, path}`) when `--plot` was used and this corner's waveform rendered at least one signal. |
| `monte_carlo`    | object \| null   | `null` unless this corner is a Monte Carlo sample, else `{sample_index, seed, process_seed, mismatch_seed}` — this sample's index and its derived seed components (`seed` is the combined value written as `.options seed=` in the generated deck). See "Monte Carlo sampling" above for the seed contract and negative-control guarantee. |

### `coverage`

Additive field (issue #1996, no `schema_version` bump), implementing the
shared vacuous-verdict convention defined once in
[`../json-contract.md`](../json-contract.md) — `klt drc` and `klt pex` emit
the same two roll-up keys for their own equivalents.

```json
{
  "coverage": {
    "corners_simulated": 8,
    "measurements_declared": 3,
    "measurements_with_limits": 2,
    "unrecognized_limit_keys": [],
    "nothing_checked": false,
    "nothing_checked_reasons": []
  }
}
```

| Field                      | Type            | Description |
| -------------------------- | --------------- | ----------- |
| `corners_simulated`        | integer         | `== corner_count`, restated here so the whole coverage story is in one block. |
| `measurements_declared`    | integer         | How many `measurements[]` entries the request declared. |
| `measurements_with_limits` | integer         | How many of those ended up with a bound `klt sim` actually applies (a `min` and/or `max` that is not `null`). |
| `unrecognized_limit_keys`  | array\<object\> | `{"measurement": <name>, "keys": [<key>, ...]}` — every declared `limits` object containing a key `klt sim` does not read. Reported whenever present, even for a run that also has usable bounds: one typo'd measurement alongside several well-formed ones is still a real gap. |
| `nothing_checked`          | boolean         | Whether this run graded *nothing*, so its `status` says nothing about the design. |
| `nothing_checked_reasons`  | array\<string\> | Why. Empty exactly when `nothing_checked` is `false`. |

Two ways a `status: "pass"` can be vacuous, one reason code each:

| Reason code                | Meaning |
| -------------------------- | ------- |
| `empty_corner_matrix`      | The PVT corner matrix expanded to zero corners, so `passed`/`failed`/`errored` are all `0` and the aggregate falls through to `"pass"`. Nothing simulated. |
| `unrecognized_limit_keys`  | Every declared `measurements[].limits` object used only keys `klt sim` does not apply. **Only `min` and `max` are read**, so a typo'd bound (`{"maximum": 1.8}`) is scored as if no bound had been declared, and every measurement passes unconditionally. |

**`status` is unchanged** in both cases, exactly as before this block
existed — the field makes the emptiness legible, it does not restate the
verdict.

A request that declares **no** `limits` at all is deliberately *not*
flagged: a characterise-and-report sweep grades nothing on purpose, which is
a stated intent rather than a silent miss. `measurements_with_limits` lets a
reader draw that distinction themselves.

[`klt signoff`](signoff.md) consumes this: a `sim` envelope reporting
`nothing_checked: true` never counts as a passing check, and never backs a
`"met"` tier-report item (reason `nothing_checked`).

### Semantics and guarantees

Carried over from the spike's proposed contract (see the spike document for
the full reasoning):

- **`fail` and `error` are always different.** `error` means no trustworthy
  number exists (nonconvergence, singular matrix, timeout, netlist error, an
  unextractable measurement); `fail` means the simulator produced a
  trustworthy number and the design missed a limit.
- **Aggregate precedence: `error` > `fail` > `pass`**, at both the corner
  level (any diagnostic forces `error`, regardless of measurement outcomes)
  and the response level (any errored corner makes the whole run `error`).
  A declared Monte Carlo sigma window that falls outside the limits is the
  one way the response can be `fail` with every *corner* passing — it is a
  statement about the sampled population, not about any single run (see
  "Monte Carlo statistics" above). It never overrides `error`.
- **Deterministic expansion and ordering** — `corners` is the full cross
  product of the declared axes minus `exclude`, in axis-declaration order
  (process outermost, temperature innermost), so output is byte-stable
  across runs given the same request and the same ngspice version (`
  runtime_s` and `engine_version` are the only fields that legitimately vary
  run-to-run/machine-to-machine).
- **`margin` sign convention** — signed distance to the nearest violated or
  nearest binding limit, in the measurement's units: positive is headroom,
  negative is violation. `null` when `value` is `null`.
- **A missing measurement is an error, not a pass.** If a `.meas` yields no
  value, that corner is `"error"` with a `"measurement"` diagnostic, and the
  measurement's `value` is `null`.
- **Every corner is reported** — `corners.length == corner_count` always,
  including errored corners (with their diagnostics).
- **`errored == corner_count` means zero measurements, not `corner_count`
  rows of evidence.** `passed`/`failed` corners are the ones that actually
  produced a trustworthy result; `errored` corners (timeout, nonconvergence,
  a never-dispatched `budget_exceeded`/`orphaned` corner, …) did not (issue
  [#1686](https://github.com/2AMLogic/klayout-tools/issues/1686)). A
  response where every corner errored — the exact shape an unmeetable
  `options.timeout_s` budget produces — is a record with **no** measurement
  data in it; a downstream reader (human or agent) should treat it as "the
  sweep produced no evidence", never mistake its `corner_count` for a count
  of usable rows.
- **Reproducibility is in-band** — `environment` hashes the netlist and
  resolved model library so a stored result can be checked against the
  inputs that produced it; a `monte_carlo` request additionally makes the
  sampled seed sequence itself reproducible from `monte_carlo.seed` alone
  (see "Monte Carlo sampling" above).

## Exit codes

| Code | Meaning                                                                    |
| ---- | ---------------------------------------------------------------------------- |
| `0`  | Every corner passed.                                                         |
| `1`  | Failed to run at all — bad/malformed request, unresolvable netlist or model library, unsupported engine, unknown backend. |
| `2`  | Usage error (missing argument, bad `--format` value) — from argparse.        |
| `3`  | Ran successfully; at least one measurement failed a limit (aggregate `status: "fail"`), every corner produced a usable result. Includes a declared Monte Carlo `mean ± k*sigma` window falling outside the limits, even when every individual sample passed. |
| `4`  | At least one corner errored (aggregate `status: "error"`) — the sweep is incomplete or untrustworthy. Also covers a corner that never ran because `options.wall_clock_budget_s` was exceeded, the launching process exited, or `options.fail_fast_probe` aborted the grid (`budget_exceeded`/`orphaned`/`timeout_budget_unreachable` diagnostics) — those corners are `"error"` too, not silently omitted; see "Wall-clock budget, orphan safety, and resume" and "Timeout-budget preflight" above. |

Under `--op-lint` the same four codes keep the same *meanings* against that
mode's own verdict: `0` ran with no `error`-severity finding (a
warnings-only run still exits `0`), `3` at least one `error`-severity
finding, `4` the operating point itself produced no trustworthy result. See
"Operating-point lint" above.

This resolves the open question the spike flagged (a pass/fail/error
trichotomy doesn't fit `klt drc`'s two-way clean/violations split): rather
than reusing `drc`'s `3` for a different meaning, `sim` takes `3` for its
closest analogue (a clean run with findings) and adds `4` for the outcome
`drc` doesn't have — a broken/incomplete run that isn't a usage error. `0`/`1`/`2`
mean the same thing as every other `klt` verb.

On error (exit `1`), a concise message is written to **stderr** and nothing is
written to stdout — no Python traceback, ever.

- `--format text` (default): a plain-text line prefixed `klt sim:`.
- `--format json`: the documented JSON error envelope (see
  [`docs/json-contract.md`](../json-contract.md)):

  ```json
  { "schema_version": 1, "error": { "command": "sim", "message": "netlist not found: /abs/path/testbench.spice" } }
  ```

## Worked example

See `examples/sim/`: `generate.py` writes `testbench.spice` (a resistor
divider — no real PDK dependency, see the module's docstring for why),
`corner.lib` (a synthetic two-corner `tt`/`ss` library), and `request.json`
(a 2×2×2 process/supply/temperature matrix with one `.meas`-backed
measurement). Unlike `examples/drc/`, there is no committed golden JSON
output — `runtime_s`/`engine_version` make a byte-exact fixture flaky by
construction — but running

```
klt sim examples/sim/request.json --format json
```

reproduces the illustrative response above (deterministic `vout` values,
since the fixture's resistor divider has no engine-version-dependent
behavior).

`request-monte-carlo.json` is the Monte Carlo variant: one `tt` corner
sampled `n=20` times against `testbench-mc.spice`, whose `R1` is drawn from
`agauss(1, 0.05, 1)` — seeded by the `.options seed=` card `klt sim` writes,
the same mechanism a mismatch-aware PDK model library's own `AGAUSS`/`GAUSS`
device parameters go through, so the sample→statistics path is exercised end
to end with no PDK dependency. It declares `quantiles: [5, 50, 95]` and
`k_sigma: 3`, so

```
klt sim examples/sim/request-monte-carlo.json --format json
```

emits 20 sample corners plus the `measurements[].monte_carlo` block
documented above (mean ≈ 0.905 V, sigma ≈ 0.020 V, a `mean ± 3*sigma` window
of roughly `[0.844, 0.967]` — comfortably inside the measurement's
`[0.75, 1.05]` limits, so `sigma_window.status` is `"pass"`).

`examples/sim-remote/` is the backend-comparison companion to this example:
a real sky130 workload (31-stage ring oscillator, minutes of ngspice per
corner) run as the same 5-corner matrix on `local` and on `remote`, with
both reference reports committed — see
[the "Remote backend" section above](#remote-backend) and
[`examples/sim-remote/README.md`](../../examples/sim-remote/README.md).
