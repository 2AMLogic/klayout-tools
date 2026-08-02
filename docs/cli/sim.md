# `klt sim`

Run a SPICE process/voltage/temperature (PVT) corner matrix headlessly and
report per-corner measurement pass/fail as structured data.

```
klt sim <request.json> [-o|--outdir <dir>] [--backend <name>] [--max-workers <n>] [--format text|json]
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
| `remote.launcher_cidr` | string | Source CIDR (typically the launcher's own public IP, e.g. `"203.0.113.4/32"`) allowed inbound SSH — the design note's SSH-inbound-only network posture. Required unless `remote.security_group_id` names an existing group. |
| `remote.security_group_id` | string | Reuse an existing security group instead of creating a fresh ephemeral one. |
| `remote.subnet_id` | string | Optional subnet to launch into. |
| `remote.ssh_user` | string | SSH login user for the baked AMI. Defaults to `"ubuntu"` (the base Ubuntu 22.04 image `scripts/aws/build-remote-sim-ami.sh` builds from). |
| `remote.provider` | string | `"aws"` for v1 — present from day one so provider is data, not a code path. |
| `remote.spot` | boolean | Request a spot instance. Defaults to `true` — corners are trivially re-runnable, so spot is a natural fit. `false` requests on-demand directly. |
| `remote.max_hourly_cost_usd` | number | Optional caller-side ceiling on the *estimated* hourly rate (a transparency guardrail, not a spend cap — see the design note's "AWS-native budget boundary vs. tool-side mechanical guardrails"). If the resolved instance type's estimated cost exceeds it, provisioning fails before any billable AWS API call. |
| `remote.ssh_ready_timeout_s` | number | Overall budget waiting for SSH to become reachable after the instance reaches `running`. Defaults to 240s. |
| `remote.ssh_timeout_s` | number | Overall SSH-command timeout for the remote `klt sim` invocation itself. Defaults to a conservative fully-serial-worst-case bound (`options.timeout_s * corner_count + 120`) — the provisioned box is right-sized to run every corner concurrently, so real runs finish far faster. |

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

- **`corners.process`** (`array<string>`, optional) — opaque `.lib` section
  names passed through to `models.lib` (e.g. sky130's `tt`/`ss`/`ff`/`sf`/`fs`,
  or a mismatch variant like `tt_mm` — no schema change needed for those).
  Selecting a process corner needs a model library; `models.lib` is only
  required when this axis is present. Omit entirely for a request that
  doesn't care about process (single point, no `.lib` card emitted).
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

Either way, the *resolved* absolute path is echoed in the response's
`environment.models_lib`, and its SHA-256 in `environment.models_lib_sha256`
— a stored result can be checked against the exact model file that produced
it.

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
| `timeout`          | The per-corner `options.timeout_s` budget was exceeded; the process is killed. |
| `measurement`      | A declared `.meas` produced no value (missing, not a `"fail"` — see below). |
| `unknown`          | Anything else that prevented a trustworthy result (e.g. ngspice not installed/spawnable). |

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

## JSON schema (the contract)

**JSON is the API.** Human-readable text output is a courtesy; the schema
below is the stable contract, subject to the same rules as every other `klt`
verb — see [`docs/json-contract.md`](../json-contract.md) for the envelope
(`schema_version`, error shape, exit codes) shared across all commands.

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
| `backend`                | string            | Execution backend for the corner matrix. Defaults to `"local"` (runs corners sequentially in-process); `"local-parallel"` runs the same matrix across a bounded local worker pool; `"remote"` provisions an EC2 instance and runs it there (see "Execution backends" and "Remote backend" above). Overridable with the `--backend` CLI flag. |
| `remote.*`               | object            | Request fields for the `remote` backend (`region`, `key_name`, `ssh_key_path`, `launcher_cidr`/`security_group_id`, `subnet_id`, `ssh_user`, `provider`, `spot`, `max_hourly_cost_usd`, `ssh_ready_timeout_s`, `ssh_timeout_s`) — see "Remote backend" above. Only read/validated when `backend: "remote"` is selected. |
| `models.lib`             | string            | Model library to bind process-corner `.lib` sections from. Required only when `corners.process` is set. See "Model library resolution" above.                        |
| `models.pdk`/`pdk_root`  | string            | Resolve `models.lib` through `klt pdk find` instead of a literal path.                                                                                                 |
| `corners.process`        | array\<string\>   | Process-corner axis — opaque `.lib` section names.                                                                                                                     |
| `corners.supply_v`       | object            | Supply axis, keyed by source/`.param` name; arrays sweep together by index.                                                                                            |
| `corners.temperature_c`  | array\<number\>   | Temperature axis, degrees Celsius. Defaults to `[27]`.                                                                                                                 |
| `exclude`                | array\<object\>   | Partial corner specs dropped from the expansion.                                                                                                                       |
| `analysis`               | object, required  | `kind` (e.g. `"op"`, `"dc"`, `"ac"`, `"tran"`) and `args`, the engine-syntax analysis-card arguments. One analysis per request. `"op"` is a valid `kind`, but see `measurements[]` below — it cannot be paired with a `.meas op` card. |
| `measurements[]`         | array\<object\>   | `name` (stable response key) and `spice` (a verbatim `.meas` card), plus optional `unit` and `limits` (`min`/`max`, either optional). No `limits` -> reported, never fails. `spice`'s declared analysis type must be one ngspice's own `.MEASURE` implements (`dc`/`ac`/`tran`/`sp`) — there is no `.MEASURE OP`; a `.meas op` card is rejected up front (`SimError`), regardless of the request's own `analysis.kind`. |
| `options.timeout_s`      | number            | Per-corner wall-clock budget. Defaults to `120`. Exceeding it kills the process and yields an `error`-status corner.                                                    |
| `options.keep_artifacts` | boolean           | Retain per-corner logs/rawfiles on disk under `--outdir` (or its default) and reference them from the response. Defaults to `false`.                                   |
| `options.waveforms`      | boolean           | Capture the optional waveform artifact (see above). Defaults to `false`.                                                                                               |
| `options.max_workers`    | integer           | Worker-pool size for the `local-parallel` backend; ignored by `local`. Must be a positive integer. Defaults to a conservative estimate derived from the local CPU count (see "Execution backends" above). Overridable with the `--max-workers` CLI flag. |
| `netlist_source`         | string            | Optional caller-declared provenance of `netlist`: `"schematic"` (pre-layout, e.g. an S6 sizing netlist) or `"extracted"` (post-layout, from `klt extract`). Omit for unchanged behavior — the field is purely additive. An unrecognized value is an application error (exit 1). See "Post-layout verification" below. |

### Response

```json
{
  "schema_version": 1,
  "netlist": "testbench.spice",
  "status": "pass",
  "corner_count": 8,
  "passed": 8,
  "failed": 0,
  "errored": 0,
  "environment": {
    "engine": "ngspice",
    "engine_version": "46",
    "models_lib": "/abs/path/corner.lib",
    "models_lib_sha256": "3ccce27a...",
    "netlist_sha256": "71d273ab...",
    "netlist_source": "extracted"
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
      "artifacts": { "log": null, "raw": null, "waveform": null }
    }
  ]
}
```

#### Top-level fields

| Field           | Type            | Description                                                                                                   |
| --------------- | --------------- | --------------------------------------------------------------------------------------------------------------- |
| `schema_version`| integer         | Version of this command's JSON shape (starts at `1`; per-command, per `docs/json-contract.md`).                 |
| `netlist`       | string          | Echo of the request's `netlist`, exactly as provided.                                                            |
| `status`        | string          | Aggregate: `"pass"`, `"fail"`, or `"error"`. Precedence: `error` > `fail` > `pass`.                              |
| `corner_count`  | integer         | Number of entries in `corners` after expansion and `exclude` — always `== len(corners)`.                        |
| `passed`/`failed`/`errored` | integer | Corner counts by status.                                                                                  |
| `environment`   | object          | Reproducibility block: engine name/version, resolved model-library path + SHA-256, netlist SHA-256, and (when the request declares it) `netlist_source`. |
| `provenance`    | object          | Shared reproducibility block (`klt_version`, `klayout_version`, `pdk`, `deck`) defined once in [`docs/json-contract.md`](../json-contract.md). `pdk` is best-effort from `models.pdk` (else `null`); `deck` pins the resolved model library (`name` = its filename, `content_hash` = `sha256:` digest) when a process axis resolved one, else `null`. Complements the sim-specific `environment` block, which hashes the same library alongside the netlist. |
| `measurements`  | array\<object\> | Per-measurement rollup across all corners, including the worst case and which corner produced it.                |
| `corners`       | array\<object\> | One entry per expanded corner, always `corner_count` entries, in the deterministic expansion order.             |

#### `corners[]` entries

| Field            | Type             | Description                                                                                                                   |
| ---------------- | ---------------- | ----------------------------------------------------------------------------------------------------------------------------- |
| `corner_id`      | string           | `<process>/<supply>V/<temp>C` (e.g. `ss/1.620V/125C`); `<process>` is `"default"` and `<supply>` is `"novdd"` when that axis is absent from the request. Multiple supply rails join as `key=value` pairs. |
| `process`        | string \| null   | Process-corner section used, or `null` when the request declares no process axis.                                              |
| `supply_v`       | object           | Supply values for this corner, keyed by source/`.param` name (`{}` when the request declares no supply axis).                  |
| `temperature_c`  | number           | Temperature for this corner.                                                                                                  |
| `status`         | string           | `"pass"`, `"fail"`, or `"error"`.                                                                                              |
| `runtime_s`      | number           | Engine wall-clock time for this corner (or time-to-timeout, on a killed run).                                                  |
| `measurements[]` | array\<object\>  | `name`, `value` (number, or `null` when unextractable), `unit`, `status` (`"pass"`/`"fail"`/`"error"`), `margin`.               |
| `diagnostics`    | array\<object\>  | `{ "severity": "error"\|"warning", "code": "...", "message": "..." }` — see the classification table above. `"warning"` only occurs for a recovered `singular_matrix`/`nonconvergence` (does not affect `status`); every other code is always `"error"`. Empty for a clean run.       |
| `artifacts`      | object           | `{"log": ..., "raw": ..., "waveform": ...}`, each an absolute path or `null`. All `null` unless `options.keep_artifacts` is true; `raw`/`waveform` additionally require `options.waveforms`. Raw log text is **never** inlined into the JSON. |

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
- **Reproducibility is in-band** — `environment` hashes the netlist and
  resolved model library so a stored result can be checked against the
  inputs that produced it.

## Exit codes

| Code | Meaning                                                                    |
| ---- | ---------------------------------------------------------------------------- |
| `0`  | Every corner passed.                                                         |
| `1`  | Failed to run at all — bad/malformed request, unresolvable netlist or model library, unsupported engine, unknown backend. |
| `2`  | Usage error (missing argument, bad `--format` value) — from argparse.        |
| `3`  | Ran successfully; at least one measurement failed a limit (aggregate `status: "fail"`), every corner produced a usable result. |
| `4`  | At least one corner errored (aggregate `status: "error"`) — the sweep is incomplete or untrustworthy. |

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
