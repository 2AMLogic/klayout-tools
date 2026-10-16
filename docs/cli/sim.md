# `klt sim`

Run a SPICE process/voltage/temperature (PVT) corner matrix headlessly and
report per-corner measurement pass/fail as structured data.

```
klt sim <request.json> [-o|--outdir <dir>] [--report-out <path>] [--backend <name>] [--max-workers <n>] [--hosts <n>] [--max-units-per-job <n>] [--budget-s <seconds>] [--resume] [--fail-fast-probe] [--fail-on-diagnostic <code>] [--dry-run] [--plot <dir>] [--op-lint [--op-lint-corner <corner_id>]] [--format text|json]
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
  `local`. Capped by the host's `KLT_SIM_MAX_WORKERS`, when set (a larger
  value is clamped, not refused). See "Execution backends" below.
- `--max-units-per-job` — backend `batch`, `remote.hosts: 1` only: split the
  expanded unit list into batch jobs of at most this many units so no job
  outruns the fleet's whole-job ceiling; overrides `batch.max_units_per_job`
  (see "Three budgets, and the fleet's whole-job ceiling").
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
- `--attach JOB_ID` — re-attach to an already-submitted `batch` job (the id
  `klt sim` prints on stderr at launch) instead of submitting a new one.
  Waits for it, bounded by `batch.poll_timeout_s`, and reports it without
  uploading or launching anything. See "Waiting, job id, and re-attaching"
  below.
- `--fail-fast-probe` — before dispatching any real corner, run a short
  bounded `tran` calibration slice on the grid's first corner and abort the
  whole grid up front if the measured rate implies `options.timeout_s`
  cannot plausibly cover the full analysis window, overriding the
  request's own `options.fail_fast_probe` when given. See "Timeout-budget
  preflight" below.
- `--dry-run` — validate the request and report its expansion without
  simulating or submitting anything ([#3064](https://github.com/2AMLogic/klayout-tools/issues/3064)).
  Every pre-dispatch check a real run performs still runs (request shape,
  Monte Carlo spec, measurement forms, model/OSDI resolution, the
  `stage_model_inputs` closure and the off-host PDK check), and a failure
  exits 1 with the same message a real run gives. It then returns before
  the fail-fast probe, the output-directory lock and any backend, so no
  ngspice, SSH, S3 or EC2 is touched. Corner/Monte Carlo requests only;
  `decks` requests are refused. The JSON response is the shared envelope
  with: `dry_run: true`, `status: "valid"`, `engine`, `backend` (resolved),
  `hosts`, `netlist`, `models_lib`, `unit_count`, `units[]` (`corner_id`
  and, for Monte Carlo, `sample_index`, `rndseed`, `process_seed`,
  `mismatch_seed` -- the values the real run derives), `monte_carlo`,
  `staged_files[]` (the staged model inputs, as in
  `environment.staged_model_inputs`), and `warnings[]` (unknown top-level
  request keys; a warning only, never an error).
- `--fail-on-diagnostic` — grade any corner that emitted this diagnostic
  `code` as `status: "inconclusive"` rather than `pass`/`fail` ("the solve
  did not convincingly converge", reported as a distinct claim from "the
  circuit missed its spec"), overriding the request's own
  `options.fail_on_diagnostic` when given. Repeatable. Matches on the code
  even when the recovered-stepping rule downgraded that diagnostic to
  `severity: "warning"` — which is the point. See "Grading a recovered
  diagnostic as inconclusive" below.
- `--report-out` — also write the report envelope to this path
  ([#2975](https://github.com/2AMLogic/klayout-tools/issues/2975)),
  byte-identical to `--format json` stdout. A path ending `.gz` is
  gzip-compressed with a zeroed header (identical results give identical
  bytes); anything else is plain indented JSON. A Monte Carlo campaign
  report runs to tens of MB indented but ~30x smaller gzipped, small enough
  to commit unmodified as evidence. Stdout and exit codes (0/3/4) are
  unaffected; an unwritable path exits 1 with the error envelope. `klt
  yield` accepts the `.json.gz` file directly.
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
in its *shape* — `request.engine` is a data field, not a code path.
`"ngspice"` is the reference path; `"xyce"` is implemented as a narrower
cross-validation oracle (below); any other value is an application error
(exit 1).

**Which ngspice binary is run (issue #2423).** The executable is resolved,
not hardcoded — first runnable candidate wins:

1. `options.ngspice_binary`, when given;
2. `$KLT_NGSPICE_BINARY`, when set;
3. `ngspice` on `$PATH`.

An explicitly-named binary (option or env var) that is not runnable is a
clear, actionable error naming which source named it — never a silent
fallback to the next source. The resolved path is recorded in the response
as `environment.ngspice_binary` (alongside `engine_version`), `null` for
`engine: "xyce"` (below), which this issue adds no override for. Mirrors
`klt lvs`'s `options.netgen_binary`/`$KLT_NETGEN_BINARY`/
`environment.netgen_binary` (issue #2373). `klt sim` has no `--check`/
`--rerun` mode, so there is no drift-diff exclusion to add for this field.

**Which machine ran it: `environment.engine_host` (issue #3110).** Every
report (corner and deck mode) carries `environment.engine_host`: `"local"`
or `"worker"` (`--backend remote`/`batch`). It says which machine every
`engine*` / `ngspice_binary` field describes. For a `"worker"` report,
`engine_version` is the worker's own, and `ngspice_binary` is the worker's
observed path (`environment.remote.worker_environment.simulator.selected_path`
when its `capture` is `"ok"`), or `null` when the worker did not report one
(a runner that predates #2834, the SSH `remote` backend, or a sharded run
whose members disagree). The submitting host's resolved path is kept under
`environment.client_ngspice_binary` — present only for off-host reports. A
local report is unchanged apart from `engine_host: "local"`.

**ngspice's working directory, and `.spiceinit` compatibility mode (issue
#2520).** Every `ngspice`/`Xyce` subprocess this command spawns — per
corner, and for the two-pass fail-fast probe's own calibration slice — now
runs with `cwd` set to that run's own artifact directory (`corner_dir` under
`--outdir` when `options.keep_artifacts` is set, otherwise a private
`klt-sim-*` scratch directory that is removed once the corner finishes),
never the directory `klt sim` itself was invoked from. This matters because
ngspice reads a `.spiceinit` init file from its **current working
directory** (falling back to `$HOME`) before touching the corner's own deck
— the file where a compatibility mode such as `set ngbehavior=hsa` (needed
for vendor model decks written in HSPICE style) is set. Before this issue,
whether that mode was active depended on which directory happened to invoke
`klt`, and on whatever `$HOME/.spiceinit` (if any) existed on that host —
unreproducible, and invisible in the request document itself.

`options.ngspice_init` (array of strings, default unset) makes that mode a
property of the *request*: each string is written as one line of a
`.spiceinit` file inside the corner's own artifact directory before ngspice
runs, so `cwd=` now guarantees it is actually read.

```json
{ "options": { "ngspice_init": ["set ngbehavior=hsa"] } }
```

Unset (the default) writes no `.spiceinit` at all — behaviour is unchanged
from before this issue, modulo the `cwd=` fix itself (which only matters if
the invoking directory or `$HOME` happened to carry a `.spiceinit` of its
own). ngspice-only: the `xyce` engine has no equivalent init-file lookup, so
`options.ngspice_init` is accepted but has no effect when `engine: "xyce"`.

### Transient noise coverage (ngspice, [#2985](https://github.com/2AMLogic/klayout-tools/issues/2985))

On the **ngspice** engine a transient (`analysis.kind: "tran"`, or a `tran`
step of `analysis_steps[]`) contains **only the noise of explicit independent
sources** (`trnoise()` / `trrandom()` on `V*`/`I*` sources). ngspice generates
no device noise (BJT/HBT shot, terminal-resistance thermal, flicker; MOSFET
channel noise) and no resistor thermal noise during `.tran`, and there is no
periodic steady-state / periodic-noise analysis to fall back on (`pss` is a
compile-time experimental option, absent from typical builds). The only
intrinsic-noise analysis is the small-signal `.noise` / `.sp` family, which is
valid about one DC operating point — not for time-varying, switched or pumped
circuits. A transient-noise figure is therefore only as complete as the
sources the deck injects. This is an upstream engine limit; `klt sim` can only
disclose it.

Evidence (ngspice-46, `ngspice -b`; a BJT with `rb`/`kf` set plus two
resistors): `.tran` reports peak-to-peak 0 on every node and bit-identical
runs under different `setseed` values, while `.noise` on the same bias point
reports `onoise_total = 1.28e-02` V rms over 250 MHz – 500 GHz.

When the request runs a transient and the netlist (or its scanned `.include`
closure, including `.subckt` bodies) *declares* at least one
`trnoise`/`trrandom` source, `environment.transient_noise` is added:

```json
{"engine": "ngspice",
 "sources": [{"name": "vn", "kind": "trnoise"}],
 "intrinsic_device_noise": false,
 "intrinsic_resistor_noise": false}
```

Names are lower-cased and sorted; "declared" is lexical (all-zero amplitudes
are still reported). If part of the closure could not be scanned,
`sources_not_scanned` lists it (never an error). Requests with no such source,
or with no transient, keep `environment` unchanged. Not claimed: the Xyce
engine's behaviour has not been verified for this finding, so the field is
never emitted for `engine: "xyce"`. See also "Monte Carlo sampling" below,
whose `unseeded_sources` disclosure scans the same sources.

### Measurement output precision (`measureprec` / `numdgt`)

ngspice prints a measurement with a fixed number of digits, and `klt sim`
reports exactly the token ngspice printed (the parser converts the whole
matched token with `float()`; it never truncates). The digit count is a
property of ngspice's **output formatting**, not of solver accuracy, and it is
controlled by two *different* settings that `options.ngspice_init` can set
(each line lands in the corner's `.spiceinit`):

| Setting | Governs | Reaches |
|---|---|---|
| `set measureprec=N` | digits ngspice prints for `.meas` results (`%.*e`: N counts digits **after the decimal point** of the mantissa, not total significant figures) | `measurements[].spice` cards |
| `set numdgt=N` | digits of a `print`ed scalar | `measurements[].expr` entries (the `let`/`print` pairs) |

Consequences:

- `.options numdgt=N` in a netlist does **not** establish `.meas` precision;
  `numdgt` and `measureprec` are separate. Set both when a request mixes
  `spice` and `expr` measurements.
- Neither setting makes the solver more accurate. They only stop ngspice from
  rounding a value it already holds. Solver tolerance (`reltol`, `abstol`,
  ...) is a separate matter.
- A derived `expr` reads the engine's stored vector, not the rounded text
  printed for an earlier `expr`: with default digits `vout` prints as
  `0.6666778`, yet `(vout - 0.6666778) * 1e9` evaluates to `-22.59...`, not `0`.
- Defaults are unchanged: with no `ngspice_init`, formatting is whatever the
  installed ngspice defaults to.
- `expr` reduces a vector analysis to a **scalar** (`vecmax()`, `mean()`,
  `<expr>[0]`, ...); it is not a point-interpolation facility. To read a
  transient value at a time, use a `.meas ... FIND ... AT=` card.

Tested recipe (PDK-free; exact value `2000.1 / 3000.1 = 0.6666777774074197`).
`divider.spice`:

```
Vin in 0 DC 1
R1 in out 1k
R2 out 0 2.0001k
C1 out 0 1f
```

Transient `.meas` (separate request from the OP one, since a corner runs one
analysis):

```json
{
  "netlist": "divider.spice",
  "analysis": { "kind": "tran", "args": "1n 10n" },
  "measurements": [
    { "name": "vout", "spice": ".meas tran vout FIND v(out) AT=5n", "unit": "V" }
  ],
  "options": { "ngspice_init": ["set measureprec=12", "set numdgt=12"] }
}
```

Operating-point scalar:

```json
{
  "netlist": "divider.spice",
  "analysis": { "kind": "op", "args": "" },
  "measurements": [ { "name": "vout", "expr": "v(out)", "unit": "V" } ],
  "options": { "ngspice_init": ["set numdgt=12"] }
}
```

**Verified behaviour, by ngspice version.** Digits observed in the raw log
(`keep_artifacts`), isolated `HOME`, `NGSPICE_MEAS_PRECISION` unset:

| ngspice | `.meas` default | `.meas` + `measureprec=12` | `expr` default | `expr` + `numdgt=12` |
|---|---|---|---|---|
| 42 | `6.666778e-01` | `6.666778e-01` (**ignored**) | `6.666778e-01` | `6.666777774074e-01` |
| 46 | `6.66678e-01` (5 decimals) | `6.666777774074e-01` (**honoured**, 12 decimals) | `6.666778e-01` | `6.666777774074e-01` |

Both rows were measured live (ngspice 42 from `/usr/bin/ngspice`, ngspice 46
from `~/.local/bin/ngspice`, each via `klt sim`'s `run_sim` with
`backend="local"` and the two requests above, varying only
`options.ngspice_init`). On 46, `numdgt=12` alone leaves a `.meas` value at
`6.66678e-01` and `measureprec=12` alone leaves an `expr` value at
`6.666778e-01`; the JSON `value` is the parsed raw token in every case
(46 `.meas` + `measureprec=12`: `0.6666777774074`; 46 `expr` +
`numdgt=12`: `0.6666777774074`). The 46 row was reproduced independently
with ngspice 46 built from the upstream `ngspice-46.tar.gz` (sha256
`a0d1699a…4d64c19b`) and selected via `$KLT_NGSPICE_BINARY`, running the CLI
(`klt sim <request> --backend local --format json`) with identical tokens
and JSON values. Note that 46's `.meas` default is 5 decimals, one fewer than
42's 6.

On ngspice 42, `measureprec`, `measure_precision` and the
`NGSPICE_MEAS_PRECISION` environment variable were all tried and none changed
a `.meas` result (neither as a `.meas` card nor as a `meas` command), and
`numdgt` does not either; so on 42 a `.meas` value is capped at 6 decimals
of mantissa (7 significant figures). On such an engine, declare the quantity
as an `expr` (with `numdgt`) when more digits are needed, for any form that
`expr` can reduce to a scalar. The regression suite
(`tests/test_sim.py`, "Measurement output precision") selects its `.meas`
assertion from the `--version` banner of the binary `klt sim` will run
(resolved like the runner: `$KLT_NGSPICE_BINARY`, then `ngspice` on `PATH`),
never from what it printed: on 46 or later it requires 12 emitted decimals
and agreement with the analytic value within `1e-10` (`rel=0`); on older
engines a separate test pins that the value is identical to a measured
default run. Set `KLT_NGSPICE_BINARY` to a 46 build to run the strict case
on a host whose `PATH` ngspice is older. The report does not record the
requested or effective formatting digits (no precision field); that is
deliberately deferred, since requested settings do not prove the effective
ones for arbitrary netlists or ambient init files.

## Xyce engine

`engine: "xyce"` runs the same corner matrix through Sandia's
[Xyce](https://xyce.sandia.gov/) — a from-scratch SPICE implementation, not
a SPICE 3f5 derivative — as an independent cross-validation oracle for the
ngspice results (issue #2016, pairing #5 of oracle-tracking #2007). Same
subprocess-per-corner model, same response contract
(`environment.engine`/`engine_version` record which engine ran), a
deliberately narrower support surface; everything below that `xyce` does
not yet implement is refused **before any corner runs** with an error
naming what *is* supported:

- **Analyses**: `dc`, `op`, and `tran`. (`ac`/`sp` are ngspice-only for
  now.) There is no `.measure op` in Xyce either — the same
  `measurements[]` constraint as ngspice applies.
- **Corners**: `process` (`.lib` sections — note Xyce requires a *named*
  `.endl <section>` in the library file where ngspice accepts a bare
  `.endl`) and `temperature`. **`corners.supply_v` and `corners.params` are refused**: Xyce has
  no netlist equivalent of ngspice's `alter`/`alterparam`, so per-corner
  supply rails and parameter rows cannot be patched into the deck — keep
  them hardcoded in the netlist body.
- **Backends**: `local` and `local-parallel` only; `remote`/`batch` pin
  the ngspice toolchain and are refused for this engine.
- **Refused**: `monte_carlo` (no seed wiring),
  `options.fail_fast_probe` (reads ngspice's rawfile stream),
  `options.osdi_preload` (`pre_osdi` is an ngspice control command; Xyce
  loads Verilog-A as its own plugins — see "OSDI (Verilog-A) model
  preload"), `measurements[].expr` (`let`/`print` are ngspice control
  commands with no Xyce `.control` block to carry them — see "Measurements
  that are not `.meas` cards"), and `analysis_steps[]` (the ordered
  sequence is `alter`/analysis/`let` commands in an ngspice `.control`
  block, and no equivalent ordered-analysis contract is implemented for
  Xyce — see "Several solves per corner" below).

Two syntax divergences the deck generator handles for you (both verified
against Xyce 7.10.0; the full list with the measured ngspice-vs-Xyce
agreement lives in `docs/design/xyce-oracle.md`): Xyce has no `.control`
block, and `.temp` is a silent no-op in Xyce — the generated deck writes
`.options device temp=<T>` instead, so temperature corners behave
identically to ngspice's. The `Xyce` binary itself is an external,
PATH-discovered dependency (`scripts/install-xyce.sh` installs the pinned
build); a missing binary folds into per-corner `status: "error"`
diagnostics ("could not launch Xyce"), exactly like a missing ngspice.

## Execution backends

`request.backend` (overridable with `--backend`) selects how the expanded
corner matrix is run. Corners share nothing — each is a pure function of
netlist + models + corner — so every backend produces the **same report
JSON, in the same corner order**, for the same request; the backend only
changes how fast (and where) the sweep runs. An unsupported name is an
application error (exit 1), exactly like an unsupported `engine`.

Every backend also accepts **deck-list requests** (a request with a `decks`
list — pre-composed caller-owned decks transported, not re-composed; see
"Pre-composed decks" below), with the same backend semantics.

**Host default — `$KLT_SIM_BACKEND`.** Precedence is `--backend` >
`request.backend` > `$KLT_SIM_BACKEND` > `local`. A fleet host that must not
run SPICE grids itself sets it in its daemon environment (2am#1004: the AWS
dispatch workers run with `KLT_SIM_BACKEND=batch`), so every sweep's grid
goes to the batch fleet with no per-repo request edit. It is a *preference*:
when it names an off-host backend (`batch`/`remote`) it steps back to `local`
for a single-unit run (one corner does not repay a Spot boot), an
`engine: "xyce"` request (the off-host toolchain pins ngspice), and
`options.resume`. An explicit flag or request field is never overridden —
pass `--backend local` to keep a multi-corner run on the host on purpose.

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

**Host-level cap: `KLT_SIM_MAX_WORKERS`.** Sizing `max_workers` per request
only helps for the callers that remember to do it — and the ones that
oversubscribe a shared box (this package's own test suite, agent-driven
runs, several concurrent sweeps) are exactly the ones that don't. Set
`KLT_SIM_MAX_WORKERS` once in the shared box's environment instead
([#2286](https://github.com/2AMLogic/klayout-tools/issues/2286)):

```bash
export KLT_SIM_MAX_WORKERS=2   # nothing on this box runs a wider pool
```

The variable is a property of the *host*, not of the design, so it lives in
the environment rather than in a request document — which is also why it
bounds every entry point identically: the `klt sim` CLI, a direct
`klayout_tools.sim.run_sim(...)` library call, and `pytest` (whose
simulation fixtures call `run_sim` on the same code path the CLI does).

| `KLT_SIM_MAX_WORKERS` | `max_workers` given? | Effective worker count |
| --------------------- | -------------------- | ---------------------- |
| unset (or empty)      | no                   | `os.cpu_count() // 8`, floored to `1` — unchanged |
| unset (or empty)      | yes                  | exactly what was asked for — unchanged |
| `N`                   | no                   | `min(os.cpu_count() // 8, N)`, floored to `1` |
| `N`, request ≤ `N`    | yes                  | exactly what was asked for |
| `N`, request > `N`    | yes                  | `N` — **clamped, not refused** |

A request above the cap is clamped rather than rejected — the caller's
number is a preference about one sweep, the cap is a fact about the box —
but never silently: a one-line notice is printed to **stderr** once per
sweep, so a caller piping `--format json` to a file still sees it on the
terminal and the report JSON is unaffected.

```
klt: notice: max_workers=16 exceeds this host's KLT_SIM_MAX_WORKERS=2 cap; clamping the local-parallel worker pool to 2.
```

The cap must be a positive integer. `KLT_SIM_MAX_WORKERS=0`, `-1`, or a
non-integer such as `many` is an application error (exit 1, same as a
non-positive `options.max_workers`) rather than a silently-ignored value — a
typo'd cap that degraded to "uncapped" would defeat the point of setting it.
Unset, or set to the empty string (`KLT_SIM_MAX_WORKERS=`, what an empty
entry in a shell env file produces), means "no cap": behaviour is then
identical to a host without the variable at all.

The cap is deliberately **not** cross-process coordination — it is a static
per-process upper bound, so `K` concurrent sweeps on a capped box can still
run `K × N` workers between them. Sizing it for the number of sweeps the box
is expected to host is the operator's call.

It applies to whichever box actually runs the pool. The `remote` and `batch`
backends re-invoke `klt sim --backend local-parallel` on the provisioned
instance, so *that* host's environment decides its cap — the caller's cap
describes the caller's box and does not travel with the job.

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
launcher pushes the netlist (plus its `.include`/`.inc` closure — see
"Off-host backends stage the netlist's `.include` closure" below) and a
request-specific copy of the request over
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
| `models.pdk` | string, required for `remote` | Selects which baked-AMI PDK to provision (`remote_launcher.SUPPORTED_PDKS`: `sky130A`, `gf180mcu`, `sg13g2`). The remote host resolves its own baked model library from this field the same way a local run resolves one (`pdk.find_pdk`, via the AMI's own `$PDK_ROOT`) — do not pair it with an operator-local absolute `models.pdk_root`, which only exists on the caller's own machine. Validated up front by `remote_launcher.ami_pdk_key` — the *same* check the `batch` backend applies, see "Both off-host backends validate `models.pdk` up front" below. `sg13g2` is registered (#2573) but no AMI has been built/published for it yet (#2574) — a request will fail at `resolve_ami` with a "no published AMI" error until an operator runs `scripts/aws/build-remote-sim-ami.sh --pdk sg13g2` for the target region. |
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
`c7i.12xlarge`, 48 vCPU). **Multi-wave batching (issue #2084):** the largest
size, `c7i.48xlarge` (192 vCPU), runs at most 20 units concurrently at 8
threads each. A single-host request with more units no longer fails — the
instance is capped at `c7i.48xlarge` and the units run in
`ceil(N / 20)` sequential waves of near-equal size, the guest's worker pool
pinned to the wave capacity. This makes a `hosts: 1` run a true same-job-size
baseline for a `hosts > 1` fleet run. A request within capacity is unchanged.
A multi-wave run adds two additive fields to `environment.remote`, `waves` and
`wave_capacity`; both are absent when everything fits in one wave.
`estimated_hourly_cost_usd` stays an hourly *rate* — total cost scales with the
longer wall-clock the extra waves take. `remote_launcher.resolve_ami()` resolves a
`(pdk, region)` pair against the versioned AMI manifest
(`data/remote-sim-ami-manifest.json`, schema at
`docs/schemas/remote-sim-ami-manifest.schema.json`) produced by
`scripts/aws/build-remote-sim-ami.sh` — ngspice, the curated `sky130A`/
`gf180mcu`/`sg13g2` model decks (`sg13g2`'s recipe additionally bakes
compiled OSDI models — see that script's SG13G2 branch), and `klt` itself
are baked into the AMI, never fetched per job. Only the netlist (with its
`.include` closure) and a generated request document
(kilobytes to low megabytes) are pushed per job — plus, when the request opts
in with `options.stage_model_inputs`, its own model inputs (see "Staging the
request's model inputs" below) — by
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

**Relative includes resolve beside the netlist.** On the local backends a
relative `.include`/`.inc` target in the netlist body (e.g. a testbench's
`.include "dut.spice"`) resolves against the **netlist's own directory**, not
the invoking cwd and not the per-corner artifact directory ngspice runs in
(ngspice resolves a relative include against the including file's directory;
verified on ngspice 42 and 46, also with a relative `-o`). Off-host backends
stage and rewrite the same closure, so one committed testbench behaves the
same on `local` and `batch`. An include that cannot be found yields a
`missing_include` diagnostic naming the file, not only per-measurement
"no value" errors.

**PDK-root includes resolve on the runner.** A `.include` in the
netlist body that resolves under the PDK root (`models.pdk_root`, else
`$PDK_ROOT`) is **not** staged or rewritten: the PDK is baked into the
runner image, so the same path must exist at the same PDK root *on the
runner*. A body that includes
`/home/<you>/.volare/<pdk>/libs.tech/ngspice/design.ngspice` fails on a
runner whose image keeps that PDK elsewhere (or lacks it) with ngspice's
`Could not find include file ...`. Prefer a PDK-root-relative include
(`$PDK_ROOT/...`) or a path under the runner's PDK root. Includes *outside*
the PDK root are staged from the submitting host. When the runner cannot
resolve an include, the per-corner `diagnostics[]` carries a
`missing_include` entry naming the file (when the engine log is retrieved,
which needs `keep_artifacts`), not only `measurement ... produced no value`.
A job that fails without a usable report states `engine log unavailable`
in its `batch_job_failed` message, or carries the tail of the retrieved
`ngspice.log`. Which PDK locations a given runner image provides, and its
`klt` version, are fleet concerns owned by
[2AMLogic/2am](https://github.com/2AMLogic/2am) (see also
[#2851](https://github.com/2AMLogic/klayout-tools/issues/2851)).

**A runner older than a request feature fails the job, not the submit.**
The client has no per-feature runner capability signal: the AMI manifest
publishes only the runner's `klt_version`/`klt_ref`, which
`runner_version_check` compares by major.minor release. A request that uses
a newer feature — e.g. a `corners.process[].sections[]` `{"lib","section"}`
object (#2522) — is therefore not refused up front when the version check is
`warn`, or the manifest has no entry to compare (`batch_runner_version_unknown`).
The job then fails in seconds with `batch job <id> finished 'failed' without a
usable report: job command exited 1`, followed by the engine-log tail or
`engine log unavailable` (a submit-only credential cannot read `harness.log`).
When that is the only evidence, suspect a runner that predates a feature the
request uses, and keep `runner_version_check` at `enforce` (the default). A
real pre-submit feature check needs a machine-readable runner capability
field and is tracked with the `analysis_steps[]` equivalent in
[#3249](https://github.com/2AMLogic/klayout-tools/issues/3249).

| Field | Type | Description |
| ----- | ---- | ----------- |
| `batch.provision_script_path` | string, required (or `$KLT_BATCH_PROVISION_SCRIPT`) | Path to 2am's `infra/aws/batch-fleet-provision.sh` on *this* host. Resolution order: this field, then `$KLT_BATCH_PROVISION_SCRIPT`, then an error naming both — never a guessed path into a sibling checkout. An unresolvable script is an error raised **before any S3 write**, mirroring `remote.ssh_key_path`'s own early check. |
| `batch.bucket` | string, required (or `$KLT_BATCH_JOB_BUCKET`, or the fleet config) | The job bucket. Resolution order: this field, then `$KLT_BATCH_JOB_BUCKET`, then `BATCH_JOB_BUCKET` in the `batch-fleet.env` sitting beside the resolved provision script (2am's own fleet config, which is the value its IAM policy is bound to). The deployed bucket name is read from that fleet config at runtime and is deliberately not reproduced in this repo — it encodes deployment-specific account detail, so every example here shows the `<your-batch-jobs-bucket>` placeholder instead. No literal default is compiled into `klt`: an unresolvable bucket is an error naming all three sources. |
| `batch.jobs_prefix` | string | S3 key prefix jobs live under. Defaults to the fleet config's `BATCH_JOBS_PREFIX`, else `"jobs"`. |
| `batch.region` | string | AWS region passed to both the `aws` CLI and `launch`. Defaults to `$KLT_BATCH_REGION`, else the fleet config's `BATCH_REGION`, else the `aws` CLI's own resolution. |
| `batch.profile` | string | `aws` CLI **profile name** the submit path runs under — a name, never a credential; the key it resolves to lives in the operator's own AWS config. Defaults to `$KLT_BATCH_PROFILE`, else the fleet config's `BATCH_SUBMIT_PROFILE`, else `"batch-runner-submit"`. |
| `batch.poll_interval_s` | number | How often `status.json` is re-read. Defaults to `30`. |
| `batch.poll_timeout_s` | number | Wall-clock budget waiting for a terminal `status.json`. Defaults to the fully-serial worst case (`options.timeout_s × corner count + 120`) plus 1800s of Spot-acquisition/boot slack. On overrun every unit is reported with a `batch_poll_timeout` diagnostic (the job may still be running on the fleet — see "Failure classification" below). |
| `batch.runner_version_check` | `"enforce"` \| `"warn"` | Runner/client `klt` version-skew policy (default `"enforce"`), applied both to the pre-launch check against the AMI manifest's published runner version and to the runner-side preflight; see "Runner/client `klt` version skew" below. Any other value is rejected before any S3 write. |
| `batch.capacity_wait_s` | number ≥ 0 | Wall-clock budget, per `launch` call, for waiting out a Spot **capacity refusal** or a fleet **concurrency-cap refusal** ([#2721](https://github.com/2AMLogic/klayout-tools/issues/2721)). Defaults to `0`: one launch attempt, exactly as before. See "Capacity refusal and `batch_no_capacity`" below. A negative, non-finite, or non-numeric value is rejected before any S3 write. |
| `batch.max_job_seconds` | number > 0 | The fleet's **whole-job ceiling** the one-job pre-submit check compares against ([#2833](https://github.com/2AMLogic/klayout-tools/issues/2833)). Resolution order: this field, then `$KLT_BATCH_MAX_JOB_SECONDS`, then `BATCH_MAX_JOB_SECONDS` in the `batch-fleet.env` beside the provision script, else `3600` (the harness's own fallback when its `MaxJobSeconds` tag is absent). The value used and its source are reported; see "Three budgets, and the fleet's whole-job ceiling" below. A present but non-positive, non-finite, or non-numeric value from any source is rejected before any S3 write. |
| `batch.job_ceiling_check` | `"warn"` \| `"enforce"` \| `"split"` | What the one-job pre-submit check does when the job's conservative bound exceeds `max_job_seconds`: `"warn"` (default) submits and warns, `"enforce"` refuses before any S3 write with `error.code: "batch_job_ceiling_exceeded"`, `"split"` ([#3446](https://github.com/2AMLogic/klayout-tools/issues/3446)) submits the request as the fewest balanced jobs that each fit. Any other value is rejected before any S3 write. |
| `batch.max_units_per_job` | integer ≥ 1 | Opt-in cap on the units of one job ([#3446](https://github.com/2AMLogic/klayout-tools/issues/3446)); `--max-units-per-job` overrides it. With `remote.hosts: 1` the request is split into `ceil(units / N)` balanced jobs, even when it would have fit one. Rejected before any S3 write with `error.code: "batch_max_units_per_job_invalid"` when not a positive integer, when combined with `job_ceiling_check: "enforce"`, or when the resulting jobs would still exceed the ceiling. |

### Asynchronous submit and status (`--submit-only`, `--job-status`, [#3073](https://github.com/2AMLogic/klayout-tools/issues/3073))

A blocking `klt sim --backend batch` run prints nothing until the fleet job
ends. To hand a long run off instead:

```bash
klt sim request.json --backend batch --submit-only --format json
klt sim request.json --job-status klt-sim-0123456789ab --format json
```

`--submit-only` runs the same pre-submit gates as the blocking path (bucket
and runner-version resolution, the whole-job ceiling), uploads the job,
launches it, and exits `0` with the job's identity — it never polls and never
collects:

| Field | Description |
| ----- | ----------- |
| `schema_version` | `1` |
| `status` | `"submitted"` (this is not a sim report; there is no verdict to grade) |
| `job_id`, `bucket`, `region`, `jobs_prefix`, `job_uri` | Where the job lives |
| `launched_at` | UTC time the launch returned |
| `request_sha256` | Digest of the request document, to tie a later read back to it |
| `unit_count` | Corners × Monte Carlo samples in the job |
| `job_ceiling` | The pre-submit whole-job-ceiling assessment (same object as `environment.remote.job_ceiling`) |

It is one job only. Anything it cannot represent as one job fails **before any
S3 write** with `error.code: "submit_only_unsupported"`: a non-`batch`
backend, `--hosts`/`remote.hosts` > 1 (a sharded run would need one job id per
shard), `--plot`, or a deck-list request. `--resume` is already refused on the
batch backend.

`--job-status JOB_ID` makes one `status.json` read (the positional request only
supplies the bucket/region/profile) and exits `0` whatever the *job's* state —
the exit code describes the read, `state` describes the job:

| Field | Description |
| ----- | ----------- |
| `state` | `pending` (no `status.json` yet — the instance has not booted; not an error), `running`, `interrupted`, `done`, `failed` or `timeout` |
| `terminal` | `true` for `done`/`failed`/`timeout` |
| `detail`, `started_at`, `updated_at`, `elapsed_seconds`, `exit_code`, `tool` | As written by the fleet harness; absent ones are omitted |
| `instance` | `instance_id`, `instance_type`, `availability_zone`, `lifecycle`, `spot`, ... when published |

An unknown job id (no `job.json` under the prefix) is an error envelope with
`error.code: "batch_job_not_found"`; a malformed id is
`"batch_job_id_invalid"`. Reading an `interrupted` job re-launches nothing —
2am's own `reconcile` does that.

**No unit counts.** The fleet's `status.json` is written at state transitions
only (`fetching inputs`, `executing`, the terminal state) and carries no
completed/total unit count; `outputs/` is uploaded only after the job ends. A
status read therefore cannot say how far through the grid a running job is, and
`klt` does not estimate one from elapsed time. That needs the fleet to publish
progress first (tracked against 2AMLogic/2am).

Collecting a finished `--submit-only` job's report is `--attach`, below.

### Waiting, job id, and re-attaching ([#2978](https://github.com/2AMLogic/klayout-tools/issues/2978))

The wait for a batch job is bounded by **`batch.poll_timeout_s`** (table
above). Its default is the fully-serial worst case, so a many-unit request
with a large `options.timeout_s` can wait hours; set `batch.poll_timeout_s`
explicitly when you want a tighter bound. On overrun every unit is reported
with a `batch_poll_timeout` diagnostic and `environment.remote.job_id`
names the job, which may still be running on the fleet.

While it waits, `klt sim` writes progress to **stderr only** (so
`--format json` stdout is unchanged):

```
klt sim: batch job klt-sim-1a2b3c4d5e6f launched; re-attach with `klt sim <request> --backend batch --attach klt-sim-1a2b3c4d5e6f`
klt sim: batch job klt-sim-1a2b3c4d5e6f state=running elapsed=0s
```

The first line appears as soon as the launch succeeds, so a caller killed by
an outer wall-clock limit still knows which job it left behind. The state line
repeats on every `status.json` state change and otherwise every 5 minutes.

`klt sim <request> --backend batch --attach <job_id>` resumes such a job:
it does not stage, upload, or launch anything, polls until the job is
terminal (or `batch.poll_timeout_s` elapses), and collects and grades it
exactly as the original invocation would have. Pass the **same request** the
job was submitted from — the request supplies the unit list and the
measurement limits; it is not re-uploaded. A job id with no `job.json` in the
bucket fails with exit `1` and `error.code: "batch_unknown_job"`. `--attach`
applies to a single-job `batch` run only: it is refused for other backends,
for sharded (`hosts > 1`) runs, for deck-list requests, and with
`--batch-set` or `--submit-only`.

### Capacity refusal and `batch_no_capacity`

When 2am's `batch-fleet-provision.sh launch` finds no Spot capacity in any
pool — after its own short internal retry loop (`BATCH_LAUNCH_RETRIES`, a
2am setting `klt` does not control) — the request fails with exit `1` and,
under `--format json`, an error envelope carrying a stable
**`error.code: "batch_no_capacity"`**
([#2721](https://github.com/2AMLogic/klayout-tools/issues/2721); the optional
`error.code` field is defined in [docs/json-contract.md](../json-contract.md)
"Error shape"):

```json
{
  "schema_version": 1,
  "error": {
    "command": "sim",
    "message": "batch backend failed: batch-fleet-provision.sh launch failed (exit 1): error: no capacity in any of the 30 pools after 3 attempt(s) — ...",
    "code": "batch_no_capacity"
  }
}
```

A capacity refusal is a fleet-wide condition that usually clears within
minutes, so the code marks it as **retryable**, unlike a request error.
The shared-fleet concurrency-cap refusal (`N instance(s) already running + 1
requested exceeds BATCH_MAX_CONCURRENT_INSTANCES=M`,
[#2917](https://github.com/2AMLogic/klayout-tools/issues/2917)) is the other
transient case; it carries its own **`error.code: "batch_concurrency_cap"`**,
so a caller can tell slot contention from a Spot shortage, and is retryable
the same way. Every other launch failure (the diversification floors, a
missing `job.json`, an IAM denial) carries no `error.code` and is never
retried. The classification is a match on the provisioner's
refusal text, because the script exits `1` for every refusal and exposes no
distinct exit code; it lives in one helper
(`sim_batch._classify_launch_failure`) so a wording change on the 2am side is
a one-line fix here.

`batch.capacity_wait_s` opts into waiting instead of failing. With a positive
budget, a `batch_no_capacity` or `batch_concurrency_cap` refusal re-invokes `launch` for the **same
job id** after a jittered exponential backoff (about 30 s, doubling, capped at
10 min, each delay drawn from 50–100 % of its nominal value and never past
the remaining budget) until the budget elapses; then the request fails with
the same code, its message noting how many attempts were
made. Things worth knowing:

- **The wait is not charged to `batch.poll_timeout_s`.** The poll clock
  starts only once a launch succeeds.
- **Re-launching is safe.** A refused launch starts no instance and closes any
  launch record it opened (2am's `cmd_launch` writes a `start` record and its
  matching `end` on the Spot-shortage path; the concurrency-cap check runs
  before any record is opened), and 2am's own Spot-interruption `reconcile`
  already re-launches under an existing job id. The uploaded `job.json` and
  `inputs/` are reused, never re-uploaded.
- **The budget is per `launch` call.** A sharded run (`remote.hosts > 1`) launches one
  job per shard, so each shard waits on its own `capacity_wait_s` budget,
  concurrently. A shard whose budget runs out is reported as a `lost_shard`
  (see "Fleet sharding" below) like any other launch failure, not as a
  request-level `error.code` — its sibling shards' results are kept.
- **Fan-out:** several requests can be submitted at once with `--batch-set`
  (below).
- **`--wait-for-capacity SECONDS`** ([#3564](https://github.com/2AMLogic/klayout-tools/issues/3564))
  sets the effective `batch.capacity_wait_s` from the command line, for callers
  who do not own the request file. It takes a finite number `>= 0` and **wins
  over** the request field (`--wait-for-capacity 0` forces a single attempt even
  if the request asks for a wait). It applies to the blocking path and to
  `--submit-only`; an invalid value is refused before any S3 write, like the
  request field. It requires the `batch` backend and is refused with `--attach`,
  `--job-status`, `--batch-set` (use `--capacity-retries`) and deck-list
  requests. On expiry the run fails with the existing `batch_no_capacity` /
  `batch_concurrency_cap` code. `--submit-only`'s reported `request_sha256`
  covers the request with this override folded in.

### Submit receipt and client termination ([#3065](https://github.com/2AMLogic/klayout-tools/issues/3065))

Right after `launch` succeeds — before the first poll — the client writes
`batch-receipt.json` into the artifacts directory (`options.artifacts_dir`,
default `<request dir>/.klt/sim`; a sharded run writes
`batch-receipt-<job-id>.json` per shard). It holds `job_id`, `bucket`,
`jobs_prefix`, `region`, `profile`, `launched_at` (UTC), `request_sha256` and
`state`. `state` is `null` until the job reaches a terminal state, so a receipt
that still has `state: null` means the client died (or timed out polling) while
the job was in flight — the job id there is the one to look up before
resubmitting. A receipt that cannot be written is a stderr warning, not a
failed sweep. `--format json` stdout is unchanged.

**Terminating the client does not cancel the job.** The fleet has no cancel
primitive, so a SIGINT/SIGTERM during the wait prints one stderr line naming
the job id, bucket and region and stating that the job is left running, then
exits nonzero with `error.code: "batch_client_interrupted"`. The receipt stays
in place with `state: null`. Previous signal handlers are restored after the
wait, and signals outside the wait behave as before.

### Submitting a set of requests (`--batch-set`, [#2869](https://github.com/2AMLogic/klayout-tools/issues/2869))

A campaign of N independent `backend: batch` requests can be submitted in one
command instead of N invocations that each back off alone:

```bash
klt sim --batch-set a.json b.json c.json --max-in-flight 4 -o out/ --format json
```

- **Bounded in flight.** At most `--max-in-flight` requests (default **4**)
  run at once -- submit, launch, and poll; the rest queue and start as slots
  free. The bound is a client-side politeness knob, not the fleet's cap.
- **Capacity refusals.** Each request runs the ordinary batch path, so
  in-request waiting is `batch.capacity_wait_s` (above) via
  `sim_batch.launch_job`. A refusal that still reaches the set carries its
  `error.code` (`batch_no_capacity` / `batch_concurrency_cap`) and appears in
  `refused_codes`. `--capacity-retries N` (default `0`, off;
  [#3520](https://github.com/2AMLogic/klayout-tools/issues/3520)) additionally
  re-submits such a request up to `N` more times with doubling backoff
  (5s, 10s, ...). Only those two codes are retried -- never another failure,
  and never on local ngspice.
- **Incremental manifest.** `<outdir>/set-manifest.json` (without `-o`: the
  `.klt/sim/` directory next to the first request) is written when the set
  starts and atomically rewritten on every transition. It lists, per request,
  `index`, `request`, `dir`, `status` (`pending`/`running`/final), `attempts`,
  and `job_id`/`job_ids` once the request finished, so a client interrupted
  mid-set can re-associate the surviving directories. The aggregate envelope is
  unchanged.
- **Batch only, never local.** The backend is forced to `batch` (`--backend`
  other than `batch` is a usage error, exit `2`); a refused or failed request is
  reported, never re-run on local ngspice. `--resume`, `--plot` and
  `--op-lint` cannot be combined with it.
- **Per-request reports.** Each request writes its full single-request report
  to `report.json` in its own directory, `<outdir>/<request-stem>/` (without
  `-o`: `.klt/sim/<request-stem>/` next to the request file).
- **Invalid request.** A request that fails validation (before any S3 write) or
  is refused becomes an `error` entry; the others proceed.

The stdout payload is one aggregate envelope
([`docs/schemas/sim-set-report.schema.json`](../schemas/sim-set-report.schema.json),
`schema_version: 1`; the single-request report is unchanged):

```json
{
  "schema_version": 1,
  "status": "error",
  "request_count": 2,
  "max_in_flight": 2,
  "submitted": 1,
  "refused": 1,
  "refused_codes": ["batch_no_capacity"],
  "requests": [
    {"index": 0, "request": "a.json", "status": "pass", "exit_code": 0,
     "job_id": "klt-sim-1f2e...", "job_ids": ["klt-sim-1f2e..."],
     "report": "out/a/report.json", "error": null},
    {"index": 1, "request": "b.json", "status": "error", "exit_code": 1,
     "job_id": null, "job_ids": [], "report": null,
     "error": {"message": "batch backend failed: ...", "code": "batch_no_capacity"}}
  ]
}
```

`job_id`/`job_ids` come from each report's `environment.remote` (several ids
for a sharded request). Aggregate `status` is `pass`, `fail` (a measurement
failed, nothing errored) or `error` (any request errored or was refused, or any
request's own status was `error`/`not_checked`/`inconclusive`).

**Exit code:** the worst per-request code -- `0` all passed, `3` a measurement
failed, `4` at least one request errored or was refused (**a partial set exits
`4`**, with the report on stdout). Exit `1` means the set could not start
(e.g. an empty or invalid `--max-in-flight`) and no report was written; the
error envelope goes to stderr as usual.

### Merging per-request reports (`klt sim-merge`, [#3045](https://github.com/2AMLogic/klayout-tools/issues/3045))

```
klt sim-merge <manifest.json> <report.json>... [--format text|json]
```

When one corner grid cannot be a single request (for example the executing
runner is older than the client and cannot carry per-section libraries,
preloaded model binaries or the full model-library set), submit one request per
process corner and recombine the reports with `klt sim-merge` instead of a
per-repo script. It is a separate verb, not `klt sim merge`: `klt sim` takes a
positional request path, so a `merge` subcommand would be ambiguous with a
request file of that name.

The **manifest** is the plan, written by whoever split the grid
([`docs/schemas/sim-merge-manifest.schema.json`](../schemas/sim-merge-manifest.schema.json)):

```json
{
  "schema_version": 1,
  "shards": [
    {"id": "tt", "request": "req-tt.json", "request_sha256": "<hex>",
     "corners": ["tt/1.800V/-40C", "tt/1.800V/125C"]},
    {"id": "ss", "request": "req-ss.json", "request_sha256": "<hex>",
     "corners": ["ss/1.800V/-40C", "ss/1.800V/125C"]}
  ]
}
```

`corners` is the ordered `corners[].corner_id` list the shard's report must
carry (that is how a report is matched to its shard -- reports carry no shard
id, so two shards planning the identical list is an invalid manifest).
`request`/`request_sha256` are optional; when given, `request` (relative to the
manifest) is hashed and must equal the pin. Shard order in the manifest is the
merged `corners[]` order, so the output does not depend on the order the report
paths are given.

**What is recomputed over the union** (with the same code `klt sim` uses, never
copied from one shard): `corner_count`, `passed`/`failed`/`errored`/
`inconclusive`, `status`, `metrics`, `diagnostic_counts`, the per-measurement
rollup (`measurements[].status` and `worst_case`: minimum margin, an absent
margin worst, ties to the first corner in merged order) and `coverage`
(`corners_simulated`, `checked`/`inapplicable`/`skipped`, `nothing_checked`),
whose ids carry corner indices local to each report and are re-based to the
merged corner order. Merging N per-process reports therefore yields the report
one request over the same grid would.

**Per-report fields become arrays**, aligned with `merge.shards[]`:
`environment.netlist_sha256`, `environment.remote` (job ids; `null` for a shard
run locally) and `provenance.input.content_hash` always; any other
`environment`/`provenance` field whose value differs between shards becomes an
array too, and fields all shards agree on stay scalar. The output adds one
additive block, `merge` (`schema_version: 1`, `shard_count`, `shards[]` with
`id`, `request`, `request_sha256`, `corner_count`); the report's own
`schema_version` is unchanged
([`docs/schemas/sim-merge-report.schema.json`](../schemas/sim-merge-report.schema.json)).
`measurements[].plot` is not carried over (it names one shard's artifact).

**Refusals.** Nothing is merged from a set that is not exactly the plan. A
refusal is a verdict, so (per the "a report was emitted" rule for exits `>= 3`)
stdout carries `{"schema_version": 1, "status": "refused", "refusal": {"code",
"message"}}`:

| Exit | `refusal.code` | Meaning |
| ---- | ------------ | ------- |
| `5` | `missing_shard` | a planned shard has no report |
| `6` | `duplicate_shard` | two reports carry the same planned shard's corners |
| `7` | `unexpected_shard` | a report shares no corner with the plan |
| `8` | `corner_mismatch` | a report overlaps the plan but is not exactly one shard's corners |
| `9` | `request_mismatch` | a planned request file is unreadable or not the one the plan pinned |
| `10` | `inconsistent_shards` | shards disagree on measurement names/units/limits, the simulated netlist or `schema_version` |

When several classes apply the exit code is the first in the order `9, 8, 7, 6,
5, 10`; the message lists every problem. Other exits: `1` could not run, with the usual error envelope on stderr
(unreadable or invalid manifest/report; a Monte Carlo or deck-list report,
which cannot be merged because sample seeds and statistics depend on the whole
sample sequence), `2` usage. A merge that succeeds exits like `klt sim` for the
merged `status`: `0` pass, `3` a measurement failed, `4` errored, inconclusive
or nothing checked.

`request_sha256` pins the *planned request file*; a report does not embed the
request that produced it, so the report-to-request binding is the corner list
plus the cross-shard consistency checks above. A deterministic shard planner
(split by a corner axis, emit requests and the manifest) is not part of this
verb.

### Three budgets, and the fleet's whole-job ceiling

A batch request is bounded by three separate time limits. Only one of them is
the fleet's own, and that one is the limit that loses results when it is hit
([#2833](https://github.com/2AMLogic/klayout-tools/issues/2833)):

| Limit | Set by | Bounds | What happens when it is hit |
| --- | --- | --- | --- |
| `options.timeout_s` (default `120`) | The request | **One unit** (one corner or Monte Carlo sample) on the job instance | That unit is killed and reported with its own timeout diagnostic. The other units still run. |
| `options.wall_clock_budget_s` | The request | The **in-worker dispatch loop**: the job's own `klt sim --backend local-parallel` stops starting units once the budget is spent, and writes a complete report with the rest marked `budget_exceeded` | Only applies if the worker reaches that point. It does **not** save a report when the fleet kills the enclosing job first. `--budget-s` overrides it and is written into the request the job receives ([#3167](https://github.com/2AMLogic/klayout-tools/issues/3167)). |
| Fleet whole-job ceiling (`batch.max_job_seconds`) | 2am's fleet (`BATCH_MAX_JOB_SECONDS`, default `3600`) | The **whole job**. The harness kills the job command at this age, and the instance powers off soon after. | `status.json` reports `state: "timeout"`, `exit_code: 124`. The interrupted `report.json` is not a complete report, so **every** unit of the job comes back `batch_job_timeout`. Units that had finished are not recovered (see "Failed jobs" below). |

**Pre-submit check (one-job requests).** Before the first S3 write, a
`remote.hosts: 1` batch request is checked against the ceiling. The check
uses the job's **conservative bound**, which is the same number `job.json`'s
`timeout_seconds` carries:

```
job_bound_s = options.timeout_s × units + 120
```

This assumes every unit uses its full `options.timeout_s`, one after another.
It is derived from the request alone and is an upper bound, **not** a measured
or predicted ngspice runtime. A job whose units finish quickly can run far
below it. The bound is compared with `max_job_seconds`. A bound equal to the
ceiling passes. When the bound exceeds the ceiling:

- under `batch.job_ceiling_check: "warn"` (the default), the client prints
  `klt sim: warning: batch job bound ...` to stderr before submitting, then
  submits as before;
- under `"enforce"`, the request fails with exit `1` and
  `error.code: "batch_job_ceiling_exceeded"` before anything is uploaded or
  launched;
- under `"split"` (or with `batch.max_units_per_job` / `--max-units-per-job`
  set), the request is rerouted through the shard path below instead: see
  "Opt-in auto-split".

Either way, the message names the bound, the ceiling and its source, and the
smallest `remote.hosts` value whose largest shard fits within the ceiling.
That value is `ceil(units / max_units_per_job)`, where
`max_units_per_job = floor((max_job_seconds - 120) / options.timeout_s)`.
If even one unit's bound exceeds the ceiling, no `hosts` value can help, and
the message says to lower `options.timeout_s` to at most
`max_job_seconds - 120` instead.

Every one-job batch report also records the assessment as
`environment.remote.job_ceiling`, whether or not the ceiling was exceeded. The
field is additive:

```json
"job_ceiling": {
  "basis": "conservative_serial_bound",
  "unit_count": 63,
  "per_unit_timeout_s": 120,
  "overhead_s": 120.0,
  "job_bound_s": 7680.0,
  "max_job_seconds": 3600.0,
  "max_job_seconds_source": "default",
  "exceeds_ceiling": true,
  "check": "warn",
  "max_units_per_job": 29,
  "recommended_hosts": 3,
  "warning": "batch job bound 7680s (options.timeout_s 120s x 63 units + 120s; ...) ... Set request.remote.hosts to at least 3 ..."
}
```

`max_job_seconds_source` is one of `"request"` (`batch.max_job_seconds`),
`"env"` (`$KLT_BATCH_MAX_JOB_SECONDS`), `"fleet_config"`
(`BATCH_MAX_JOB_SECONDS` in `batch-fleet.env`), or `"default"` (`3600`,
the value 2am's harness falls back to when the instance's `MaxJobSeconds`
tag is absent). `warning` is present only when `exceeds_ceiling` is `true`.
`recommended_hosts` is `null` when no `hosts` value fits.

**The mitigation is the existing shard path.** Worked example: the report
in #2833 had 63 corners at the default `options.timeout_s: 120`, so the bound
is `120 × 63 + 120 = 7680 s`, above `3600 s`. At most
`floor((3600 - 120) / 120) = 29` units fit in one job, so the recommendation
is `remote.hosts: 3`. That gives shards of 21 units each, and each shard's
bound is `120 × 21 + 120 = 2640 s`, within the ceiling:

```json
{
  "backend": "batch",
  "remote": { "hosts": 3 },
  "options": { "timeout_s": 120 },
  "batch": { "job_ceiling_check": "enforce" }
}
```

`remote.hosts > 1` submits one independent job per shard and merges them in
global unit order (see "Fleet sharding" below). An explicit `remote.hosts > 1`
is honored as given: the check, and the split below, never apply to it, and its
shards are not assessed. Each extra shard is one more
launch against the fleet's shared concurrency cap. Because the bound is
conservative, a tighter `options.timeout_s` that still covers each unit (see
"Timeout-budget preflight" below) often needs far fewer shards than a
generous one.

**Opt-in auto-split.** Rather than hand-picking `remote.hosts`, set
`batch.job_ceiling_check: "split"` or a cap (`batch.max_units_per_job`,
`--max-units-per-job N`). With `remote.hosts: 1` the client then submits
`ceil(units / cap)` balanced jobs through the shard path (the cap defaults to
the largest unit count that fits the ceiling), merged in global unit order
with the same report shape as `remote.hosts: <that many>`, and prints
`klt sim: notice: splitting ...` to stderr. In the 63-unit example above that is
3 jobs of 21 units. A request that already fits stays one job unless a smaller
cap is given. Splitting is never silent: each extra job is one more launch
against the fleet's shared concurrency cap, which is why it is opt-in and
the default stays `"warn"`. A cap that would leave a job over the ceiling is
rejected up front (`batch_max_units_per_job_invalid`), and when even one unit
exceeds the ceiling the "lower `options.timeout_s`" refusal applies
(`batch_job_ceiling_exceeded`), because splitting cannot help. When a split
mode is active, `environment.remote.job_ceiling` gains `split` (boolean) and
`effective_hosts`; the report otherwise carries the `fleet[]` array of a
`remote.hosts > 1` run. Units that finished before a killed job are still not
recovered (the harness never writes an incremental report).

### Both off-host backends validate `models.pdk` up front

`remote` and `batch` (`sim._OFFHOST_BACKENDS`) both run on images the same
AMI pipeline publishes, so both accept exactly the same PDK set
(`remote_launcher.SUPPORTED_PDKS`: `sky130A`, `gf180mcu`, `sg13g2`) and both
refuse an unsupported one **before spending anything** — `remote` before its
`run-instances` call, `batch` before its first S3 write
([#2523](https://github.com/2AMLogic/klayout-tools/issues/2523)):

```
unsupported PDK 'sky130B' for the batch backend (supported: sky130A, gf180mcu). ...
```

The check is one function, `remote_launcher.ami_pdk_key`, called by both
paths rather than restated per backend, so the two can never disagree about
what is supported. Consequences worth knowing:

- **A variant reducible to a published family is accepted** on both, on the
  same terms: `models.pdk: "gf180mcuC"` → family `gf180mcu`, or
  `models.pdk: "ihp-sg13g2"` → family `sg13g2`. The *variant* name is what
  keeps flowing off-host (`job.json`'s `pdk_variant`), because the executing
  box resolves the PDK locally exactly as the `local` backend does.
- **A variant whose family has no published image is refused even though it
  resolves locally** — `sky130B` reduces to `sky130`, which is not a
  published key (only `sky130A` is), so it is named rather than silently
  served a different model deck.
- **A request naming no `models.pdk` at all is untouched.** Without a
  `corners.process` axis there is no model library to resolve, so there is
  nothing to validate; such a request still submits.

**The seam is 2am's, verbatim** (`infra/aws/batch-fleet.md` §"The seam: what
a consumer-repo wrapper does"), and `klt` implements exactly its four steps:

1. **write** `inputs/netlist.cir` + `inputs/request.json` (plus one
   `inputs/<name>` per staged `.include`/`.inc` target — see "Off-host
   backends stage the netlist's `.include` closure" below), then
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
`outputs/**` and `status.json` are). The generated `job.json` therefore
redirects stdout with `> "$EDA_OUTPUT_DIR/report.json"`. Because klt writes its
`--format json` *error* envelope to stderr, the command also tees stderr into
`$EDA_OUTPUT_DIR/stderr.log` (`2> >(tee ... >&2)`, which still forwards it to
`harness.log`) and ends with `; rc=$?; wait $! 2>/dev/null; exit $rc` so the
tee drains before upload and the job keeps klt's own exit code.
`--outdir "$EDA_OUTPUT_DIR/artifacts"`
is added when `options.keep_artifacts` is set so per-corner logs/rawfiles
land inside the one tree the harness collects. The generated document's
schema is [`docs/schemas/batch-job-spec.schema.json`](../schemas/batch-job-spec.schema.json).

**`options.resume` is supported for `batch`** (unlike `remote`). The job's
reports are recorded into the checkpoint after the job returns, keyed by
`corner_id`, except corners that never ran: those carrying a `lost_shard`
(Spot capacity lost), `batch_job_failed`, `batch_job_timeout` or
`batch_poll_timeout` diagnostic. A re-run with `--resume` submits only those
corners and merges them with the checkpointed reports in the request's
corner order; the checkpoint is retained while any of them remains. Corners
that ran and errored are checkpointed like on `local`.
Works for `hosts == 1` and `hosts > 1`.

The dispatch-time fail-fast probe is skipped for `batch` —
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
  "elapsed_seconds": 412,
  "runner_klt_version": "0.9.0",
  "client_klt_version": "0.9.0",
  "runner_compatibility": "match",
  "client_submitted_at": "2026-10-15T17:00:00.120000Z",
  "client_launched_at": "2026-10-15T17:00:02.480000Z",
  "client_status_terminal_at": "2026-10-15T17:08:31.050000Z",
  "client_collected_at": "2026-10-15T17:08:33.910000Z"
}
```

The four `client_*_at` fields are **client-observed wall-clock times**
(RFC3339 UTC), read on the submitting host, so they are subject to its clock
skew. Unlike `elapsed_seconds` (the runner's view, from `status.json`) they
show where the wait went:

| Field | Measured |
|---|---|
| `client_submitted_at` | immediately before the job contract is uploaded |
| `client_launched_at` | just after the launch call returns |
| `client_status_terminal_at` | when polling saw a terminal `status.json` state |
| `client_collected_at` | after the job's outputs were collected (`null` when nothing was collected) |

A phase that was not reached is `null`, never fabricated: a poll-timeout run
reports only the first two, a failed job the first three. Under sharding every
`fleet[]` entry carries its own set. Queue/provisioning time is approximately
`client_status_terminal_at - client_submitted_at - elapsed_seconds`; it is an
approximation, since it also includes upload, launch and polling latency.

### Request features and minimum runner version ([#2877](https://github.com/2AMLogic/klayout-tools/issues/2877))

Older fleet runners reject some newer request fields loudly (for example
`each request.measurements[] entry requires 'name' and 'spice'` on 0.5.0) and
silently ignore others. The minimum `klt` release that understands each field
is recorded in `klayout_tools.sim_batch.REQUEST_FEATURE_MIN_RUNNER_VERSION`;
this table is tested against it. `unreleased` means the feature is on `main`
but no tagged release contains it yet.

| Request feature | Minimum runner `klt` |
| --- | --- |
| `measurements[].expr` | 0.7.0 |
| `corners.process[].sections[]` (object form) | 0.7.0 |
| `options.ngspice_init` | 0.7.0 |
| `options.osdi_preload` | 0.7.0 |
| `analysis_steps[].measurements[]` (noise analysis `.meas` cards) | unreleased |

Safe on an old runner: a plain `measurements[]` `{name, spice}` `.meas` card
and the plain-string `sections[]` form. Prefer them over `expr` when the
fleet image is older than the client. Do not rely on `options.ngspice_init` /
`options.osdi_preload` against an older image: they may be silently ignored.

In `expr` expressions, spell comparisons `lt` / `gt`, not `<` / `>`: ngspice 42
parses `<` inside a `let` as a redirect while 46 accepts it, and `lt`/`gt`
work on both ([#2940](https://github.com/2AMLogic/klayout-tools/issues/2940)).

### Runner/client `klt` version skew ([#2719](https://github.com/2AMLogic/klayout-tools/issues/2719))

The job runs the `klt` baked into the fleet image, which can be older than the
submitting client. An older runner fails loudly on some newer request fields
but silently ignores others (for example `options.ngspice_init`), so the same
request could run with different simulator settings than a local run and still
report a pass. Policy: **reject before simulating**.

- Every job command starts with a client-generated shell preflight that uses
  only `klt --version` (so it works on images that predate any new request
  key). It records the runner's build string in `outputs/runner_identity.json`
  and compares it with the client's `klt --version` by **major.minor**
  ([#3184](https://github.com/2AMLogic/klayout-tools/issues/3184)): the
  first two components of the release segment, which is the version with its
  PEP 440 local part (`+...`) and any pre/dev/post suffix removed. Patch
  releases, `.devN` builds and `+g<sha>` local builds all count as the same
  version as their major.minor. The strings klt produces are:

  | build | `klt --version` | major.minor | against a `0.7.0` runner |
  |---|---|---|---|
  | clean tree on the `v0.7.0` tag | `0.7.0` | `0.7` | match |
  | any commit after `v0.7.0` | `0.7.1.devN+g<sha>` | `0.7` | match |
  | either of the above, uncommitted changes | `...+g<sha>.dirty` | `0.7` | match |
  | no git facts | `0.7.0.dev0+unknown` | `0.7` | match |
  | patch release | `0.7.3` | `0.7` | match (either direction) |
  | next minor, released or in development | `0.8.0`, `0.8.0.devN+g<sha>` | `0.8` | **mismatch, exit 87** |

  A version whose release segment has fewer than two components (for
  example `7`) has no major.minor and counts as unknown. Both full build
  strings are kept in `runner_identity.json`, the mismatch message and
  `environment.remote`.

  The rule depends on a release discipline: **a change that breaks the
  runner contract ships in a minor release, never a patch, and every minor
  release is published to PyPI and gets a rebaked runner image.** The
  accepted cost is that a dev client can send request keys that the runner's
  release of the same minor does not have; such changes belong in the next
  minor.
- On a mismatch (`batch_runner_version_mismatch`) or a runner version that is
  absent or has no major.minor (`batch_runner_version_unknown`) the
  preflight writes a klt error
  envelope to `report.json` and exits `87` **without running `klt sim`**. The
  client recovers it through the normal failed-job path, so every unit gets a
  `batch_job_failed` diagnostic with `runner_code` set to the code above, the
  runner's message, and `runner_klt_version` / `client_klt_version`.
- `environment.remote` (and each `environment.remote.fleet[]` entry for
  `hosts > 1`, so mixed-version shards stay distinguishable) always carries
  `runner_klt_version` (the runner's own version, or `null` when it could not
  be established -- never the client's), `client_klt_version`, and
  `runner_compatibility` (`match` / `mismatch` / `unknown`; `unknown` also
  when either version has no major.minor). A non-`match`
  value also carries `runner_compatibility_warning`. The report's
  `provenance.klt_version` stays the client's.
- `batch.runner_version_check` (`"enforce"`, default, or `"warn"`) selects the
  behavior. `warn` runs the simulation anyway and only reports the skew in
  `environment.remote`; use it deliberately (for example a client on a newer
  release than the image), because it is detection after execution and does
  not guarantee request options were honored.

```json
"remote": {
  "provider": "aws-batch-fleet",
  "job_id": "klt-sim-4f2b19c0ae31",
  "state": "failed",
  "exit_code": "87",
  "runner_klt_version": "0.4.2",
  "client_klt_version": "0.9.0",
  "runner_compatibility": "mismatch",
  "runner_compatibility_warning": "the fleet runner ran klt 0.4.2 but the submitting client is 0.9.0; ..."
}
```

Limits: a major.minor match does not prove equivalent behavior between two
builds of one minor (a `0.7.1.devN+g<sha>` client or a `0.7.3` release can
carry changes the `0.7.0` runner lacks; by the release discipline above, none
of them may break the runner contract). **Remediation** on a mismatch:
update the runner image to the client's minor, or submit from a client of the
image's minor. Jobs that never reach a terminal state (`batch_poll_timeout`)
report `runner_klt_version: null` and `runner_compatibility: "unknown"`.

#### Request-feature capability check ([#3249](https://github.com/2AMLogic/klayout-tools/issues/3249))

The major.minor gate lets a post-tag client (`0.7.1.devN`) talk to a `0.7.0`
runner, so `runner_compatibility: match` does not mean the runner can parse
every field the request carries. A small registry of request features that
landed after a release is therefore compared against the runner's **full**
release segment:

| Feature | Detected when | First runner release |
|---|---|---|
| `analysis_steps` ([#2482](https://github.com/2AMLogic/klayout-tools/issues/2482)) | `analysis_steps` is present | `0.7.1` |

`decks[]` ([#3303](https://github.com/2AMLogic/klayout-tools/issues/3303)) is
the next registry entry.

- **Before launch**: when every image the AMI manifest publishes predates a
  feature the request uses, the request is refused once with
  `batch_runner_feature_unsupported` naming the feature and both versions.
  Nothing is uploaded or launched.
- **On the runner**: after the version gate passes, the preflight exits `87`
  with the same code before `klt sim` runs; each unit gets a `batch_job_failed`
  diagnostic with `runner_code: "batch_runner_feature_unsupported"` plus
  `runner_klt_version` / `client_klt_version`.
- **`environment.remote.runner_request_features`** (only when the request uses
  a registry feature) holds `required`, `missing` (`null` when the runner
  version is unknown) and `status` (`supported` / `unsupported` / `unknown`).
  An `unsupported` status also sets `runner_compatibility_warning`, even when
  `runner_compatibility` is `match`.
- **Unknown degrades to a warning**: an unreadable runner version, or a legacy
  manifest entry without `klt_version`, is never treated as missing.
  `runner_version_check: "warn"` reports without refusing.
- Limit: a `0.7.1.devN` runner built before a feature landed looks the same as
  one built after, so it counts as supporting it.

#### Pre-launch check from the AMI manifest ([#2851](https://github.com/2AMLogic/klayout-tools/issues/2851))

The runner-side preflight only runs once a Spot instance has booted. To catch
a known mismatch before that, the client also checks the runner `klt` that
the **AMI manifest** publishes. This is the same `remote-sim-ami-manifest.json`
that `remote` resolves its AMI from, found by the same search order:
`request.remote.ami_manifest` is not consulted here, so the order is
`$KLT_AMI_MANIFEST`, then `~/.config/klt/remote-sim-ami-manifest.json`, then
the packaged `data/` copy. See
[`docs/schemas/remote-sim-ami-manifest.schema.json`](../schemas/remote-sim-ami-manifest.schema.json).

- **Source.** Each manifest entry may carry `klt_version` (the build string
  the image's `klt --version` reports, without `klt `) and `klt_ref` (the git
  ref it was installed from, for audit only).
  `scripts/aws/build-remote-sim-ami.sh` publishes both for every new or
  copied image. `klt_ref` is the pinned `--klt-ref` that the build installs
  `klayout-tools` from, and `klt_version` is what that install reported on
  the build instance. The script refuses to publish an image whose version
  it could not read. `--emit-manifest-entry <ami_id> <klt_version>` prints
  the entry a build would write, with no AWS call. Entries written before
  #2851 have neither field, which means unknown.
- **Which images.** For each `(pdk, region)` pair, the check uses the most
  recently built entry, the same entry `resolve_ami()` would pick. Only the
  request's `models.pdk` is considered (every PDK when it names none), and
  only `batch.region` (every region when none is configured, because the
  fleet chooses its own Spot pool).
- **Rule.** The check uses the same major.minor release-segment comparison
  as the runner-side gate (#3184, table above), so the two can never
  disagree. It reports a mismatch only when **every** candidate image
  publishes a `klt_version` and none of them matches the client.
- **`enforce` (default).** A known mismatch fails the whole request once,
  before any S3 write or launch call. It exits `1` with a top-level
  `error.code` of `batch_runner_version_mismatch` (the existing code). The
  message names every candidate image with its full `klt_version` and
  `klt_ref`, plus the client's full version. No instance is launched, no
  report or `corners[]` is produced, and there are no per-corner
  `batch_job_failed` diagnostics. A sharded (`hosts > 1`) request is checked
  once, not once per shard.
- **`warn`.** Prints the same message to stderr and submits as before. The
  runner-side preflight then runs in its `warn` mode.
- **Unknown.** If no manifest is found, no image is published for the
  request, any candidate is a legacy entry without `klt_version`, a version
  is unreadable, or the candidates disagree, nothing is refused locally. The
  request is submitted unchanged and the runner-side preflight above
  decides, exactly as before #2851. No historical manifest entry becomes
  unusable.
- **PDK variant (#3251).** A manifest entry may publish `pdk_variants`, the
  PDK variant directories its image ships (e.g. `["gf180mcuD"]`;
  `build-remote-sim-ami.sh --pdk-variants`). The family-level check only maps
  `gf180mcuC` to `gf180mcu`, so without this field an unshipped variant fails
  on the runner as one `batch_job_failed` per corner. When **every** candidate
  image (same selection as above) publishes `pdk_variants` and none lists the
  request's `models.pdk`, the whole request is refused with a `SimError`
  naming the variants the image carries, before any S3 write or launch. A
  legacy entry without the field, an absent manifest, or a request without
  `models.pdk` is unchanged.
- **Discovering the runner version before submit.** Read the manifest:
  `jq '.images[] | {pdk, region, ami_id, built_at, klt_version, klt_ref}'
  "${KLT_AMI_MANIFEST:-$HOME/.config/klt/remote-sim-ami-manifest.json}"`.
  For a given pair, the entry with the latest `built_at` is the one in use.
- **Limit.** A manifest `klt_version` is publication metadata, not proof of
  the bytes a job executes. The fleet's own launch configuration, not this
  repository, decides which image boots. That is why a `match` or `unknown`
  still goes through the runner-side preflight, and why
  `environment.remote.runner_klt_version` (what the job actually ran) stays
  the authoritative record.

- **What the gate cannot see (`corners.params`, #3035).** The gate compares
  major.minor only, so a same-minor image built *before* a request field
  existed passes it and ignores that field. For `corners.params` `klt sim`
  does not rely on the version: it requires every returned corner to echo
  the dispatched `corner_id` and `params`, and fails the request with
  `params_unsupported_worker` otherwise (see "Corner axes"). Other newer
  fields still depend on the version check above.

**When the client is newer than the runner.** If the image is a different
minor from your client, either:

1. **Rebuild or publish a runner image** of your client's minor. Run
   `scripts/aws/build-remote-sim-ami.sh` with a matching `--klt-ref`, or have
   the fleet image rebuilt. This is the supported fix.
2. **Submit from a client of the image's minor.** Released `klt 0.5.0` has
   no `batch` backend at all ("unsupported backend 'batch'"). So against a
   `0.5.x` image, no client can both submit and pass the gate. Option 1 is
   the only clean path there.
3. **Set `request.batch.runner_version_check: "warn"`** to run anyway. This
   is an informed override, not a compatibility guarantee. The older runner
   may reject or silently ignore request options it does not know, and the
   run can still report a pass. Field-level compatibility under `warn` is
   tracked in [#2901](https://github.com/2AMLogic/klayout-tools/issues/2901),
   and the request-feature minimum-version registry in
   [#2877](https://github.com/2AMLogic/klayout-tools/issues/2877). Check
   `environment.remote.runner_compatibility` and
   `runner_compatibility_warning` in the result.

### Worker-observed environment (`worker_environment`, [#2834](https://github.com/2AMLogic/klayout-tools/issues/2834))

`environment.remote` (and every `environment.remote.fleet[]` entry for
`hosts > 1`) carries an additive `worker_environment` object describing what
the **job instance** resolved -- distinct from the client-scoped
`provenance` / `client_klt_version` fields, which describe the submitter. The
same runner preflight that writes `runner_identity.json` also writes
`outputs/worker_environment.json` (no second preflight), before the
version-skew gate, so a rejected job still leaves it behind. Capture is
best-effort and cannot change the job: bounded probe timeouts, an
allowlist of observations (never the whole environment or arbitrary manifest
contents), size-capped, serialised with a JSON encoder, errors recorded as
`diagnostics`, and the capture step can never fail the job.

| Field | Meaning |
| --- | --- |
| `capture` | `ok`, `absent` (older worker, no artifact, poll timeout) or `malformed`. When not `ok`, all observations are `null` -- never the client's values. |
| `simulator` | `requested_binary`, `binary_source` (`request` / `KLT_NGSPICE_BINARY` / `PATH`), `selected_path` (binary actually selected on the worker), `version_banner` (full `--version` text, bounded to 2000 chars), `version_probe` (`ok` / `timeout` / `exit_N` / `error` / `unavailable`). |
| `model_inputs` | Under `options.stage_model_inputs`: `[{name, sha256}]` digests of the staged model files in `$EDA_INPUT_DIR` (bounded to 64); `null` otherwise or when an older worker does not report it. Lifted to `environment.remote.runner_model_inputs`. |
| `pdk` | `variant`, worker `root`, `stamp_scheme` (`sources-sha256`), `stamp_sha256`, `stamp_text` (bounded `SOURCES` text) for the resolved variant, `models_staged` (true under `options.stage_model_inputs`: the executed models then came from the request, not the image tree). Stamp fields are `null` when `SOURCES` is absent. |
| `cpu_model` | First `model name` from the worker's `/proc/cpuinfo`, else `null`. CPU count stays `physical_cores` / `concurrency` from `status.json`. |
| `omp_num_threads` | The worker's effective `OMP_NUM_THREADS`, `null` when unset. |
| `image_manifest` | Allowlisted fields of a worker-local manifest (`$KLT_IMAGE_MANIFEST`, `/etc/klt-image-manifest.json`, `/opt/klt/image-manifest.json`), or `null` when none exists. This is the worker's own file, not the submitter's AMI selection metadata (`ami_id`, `data/remote-sim-ami-manifest.json`); a selected image is not evidence of the model bytes executed. Whether an image pins its PDK revision is reported only if the worker manifest says so (`pdk_pinned`); otherwise unknown. |
| `diagnostics` | Bounded list of capture problems (missing binary, unreadable stamp, ...). |
| `pdk_stamp_comparison` | `status` of `match`, `mismatch` (with `warning`) or `not_comparable`. |

`pdk_stamp_comparison` compares the worker's stamp with the `SOURCES` stamp the
submitter resolves locally (`models.pdk_root` or `$PDK_ROOT`) and reports a
`mismatch` only when both sides give the **same variant under the same stamp
scheme**; a missing stamp or an unlike scheme is `not_comparable`, never a
proven mismatch. A matching stamp does not prove model-closure equality
(see #3071), and a matching major version (`engine_version`, the top-level
simulator version) does not imply the same build: compare `version_banner`
and `selected_path` for build identity.

Coverage: the `batch` backend only, single jobs and every sharded member
(mixed worker identities stay individually visible; recovered failed-job
artifacts are included). The `remote` (SSH) backend does not write this
capture yet and reports no `worker_environment`. Workers that predate the
capture, or lack `python3`, report `capture: "absent"`.

### Failed jobs: output recovery and the diagnostic you get

When a job reaches a terminal `failed`/`timeout` state with an exit code
outside `klt sim`'s own `0`/`3`/`4`, the client makes **one bounded,
collection-only attempt** to download `outputs/` and read `report.json`; when
that holds no error envelope, it also reads the tail (at most 64 KiB) of
`stderr.log` and parses the trailing JSON object starting at column 0 — the
shape klt's `emit_error` writes — ignoring any warning lines before it. It
never resubmits or re-launches the job. The collected object is classified
the same way whichever file it came from:

| Collected `report.json` / `stderr.log` envelope | Result |
|---|---|
| klt error envelope (`{"schema_version": 1, "error": {"command", "message"[, "code"]}}`, see [json-contract](../json-contract.md)) | Every requested corner is reported with the job's `batch_job_failed`/`batch_job_timeout` diagnostic whose `message` carries the runner's own `error.message`; `error.code`, when present, is attached as `runner_code`. |
| Missing, invalid JSON, non-object, unrelated object, empty/invalid `corners`, or an ordinary report on a non-sim exit code — and no envelope in `stderr.log` | Existing fallback (`... without a usable report`), with a short `[output recovery: ...]` note saying why recovery found nothing and pointing at `harness.log`. |
| Download itself fails | Same fallback, the collection error is noted; the primary job failure is never replaced. |

A `done` job (or exit 3/4) whose collection fails still raises as before. A
`batch_poll_timeout` is not a terminal failure (the job may still be
running), so no recovery is attempted. Raw logs are never copied into the
JSON; use the retained artifacts below.

While a job is waiting, `klt sim` writes one progress line per state change
to **stderr** (never stdout, so the JSON report is unchanged):
`klt sim: batch job <job_id>: submitted` / `launched` / `running` /
`interrupted` / the terminal state. Unchanged polls print nothing. A sharded
request (`remote.hosts` > 1) prints one such stream per shard; the job ids
tell them apart. A job that has published no `status.json` 1800s after
launch (instance never launched or booted, e.g. a saturated fleet cap) ends
as `batch_poll_timeout` saying it never started, instead of waiting out the
serial `batch.poll_timeout_s` budget; a job whose `status.json` appeared
keeps that budget.

A job killed at the fleet's whole-job ceiling is the common `timeout` case.
Its `report.json` is truncated or missing, so every unit stays
`batch_job_timeout`. The client never parses a partial report, and it never
infers that a unit completed from status timing. To avoid this, size the job
before submitting it; see "Three budgets, and the fleet's whole-job ceiling"
above.

**Note.** Only the envelope's structured `message`/`code` reach the JSON;
raw `stderr.log` text never does. The stderr capture is part of the command
the *submitting* client generates, so it works whatever klt version the fleet
image runs. When neither file yields an envelope, the reason is in
`harness.log` (and `stderr.log`) under the job's S3 prefix.

### Portable deck and log (`options.portable_artifacts`, [#3052](https://github.com/2AMLogic/klayout-tools/issues/3052))

With `options.keep_artifacts`, `corner.cir` and the engine log
(`ngspice.log` / `xyce.log`) name the absolute paths of the machine that ran
them. Set `options.portable_artifacts: true` (boolean, default `false`; a
no-op without `keep_artifacts`) to have klt rewrite them, after the corner
finishes, so they can be committed as the raw record of a run:

| Token | Stands for |
|---|---|
| `$CORNER_DIR` | the corner's own artifact directory |
| `$PDK_ROOT` | `$PDK_ROOT` from the environment, when set |
| `$MODELS_DIR` | the directory of `models.lib` |
| `$SECTION_LIB_DIR_<n>` / `$OSDI_DIR_<n>` | further `corner_section_libs` / `osdi_preload` directories |
| `$REQUEST_DIR` | the directory holding the request file |
| `$ARTIFACTS_DIR` | the artifacts root (`--outdir`) |
| `$HOME` | the user's home directory |

Where roots nest, the earlier row wins (so a model file under `$PDK_ROOT` is
`$PDK_ROOT/<rel>`); a root is replaced only as a whole path prefix, and its
symlink-resolved form is replaced too. The same substitution is applied to
`netlist.expanded.cir` (the include-expanded netlist copy) and `.spiceinit`
in the corner directory. Paths outside every root are left as written.

The persisted `corner.cir` is **a record, not a re-runnable input**: ngspice
does not expand `$VAR` in include paths, so klt executes the real-path deck
and rewrites it only after the engine has exited. With the option off, deck
and log bytes are unchanged.

`environment.portable_artifacts.substitutions` lists each token and what it
stood for (normalised like every other path in the report, never a host
path). The inputs stay identified by `environment.models_lib_sha256` and
`environment.netlist_closure`; the `$MODELS_DIR` entry repeats
`models_lib_sha256`. Output is deterministic for a given environment: the
same request from two checkout locations yields byte-identical deck and log.

Backends: `local` and `local-parallel` apply it directly. On `batch` and
`remote` the worker runs the forwarded request, so the option applies on the
worker and the returned `artifacts/` are already portable. `klt
env-provenance scan` over a portable deck or log reports no `home-path`
findings.

### Getting the per-corner deck and ngspice log through klt

Set `options.keep_artifacts: true` (and pass `--outdir DIR` to choose where
they land). The job writes `artifacts/` inside its `outputs/`, klt downloads
it, and each corner's `artifacts.deck` / `artifacts.log` point at the local
copies under `DIR` (one `DIR/<job_id>/` subdirectory per shard when
`hosts > 1`). This works for any run that produced a simulator report,
including failed corners and exit-3/4 jobs.

Limitations: if the job failed before ngspice ran (a validation error, a
failed upload) no deck or log exists, so there is nothing to retrieve. In that
case the diagnostic and `environment.remote` give `job_id` and `bucket`;
the remaining evidence (`status.json`, `harness.log`, `outputs/`) lives at
`s3://<bucket>/<jobs prefix>/<job_id>/`.

### Post-processing waveforms from a batch run locally

Per-corner waveforms come back from `batch` the same way as from `local`, as
long as **both** `options.waveforms: true` and `options.keep_artifacts: true`
are set (`waveforms` alone captures the rawfile on the job instance, but
nothing is brought home without `keep_artifacts`). Both options are forwarded
verbatim to the job's own `klt sim`; `batch` adds no trace-related refusal.
Pass `--outdir DIR` to choose where the artifacts land:

```json
{ "backend": "batch", "analysis": {"kind": "tran", "args": "1n 1u"},
  "options": {"waveforms": true, "keep_artifacts": true} }
```

Each corner's `artifacts.waveform` / `artifacts.raw` / `artifacts.log` /
`artifacts.deck` then point at local copies under `DIR` (under
`DIR/<job_id>/` per shard when `hosts > 1`), so a local script can read the
per-corner waveform JSON exactly as it would after `--backend local`.
`options.waveforms` is refused for `analysis_steps[]` sequences on every
backend. Regression: `test_run_sim_batch_returns_per_corner_waveform_artifacts`.

### gf180 local-versus-batch troubleshooting checklist

A gf180mcuD netlist was reported to simulate locally but fail on the fleet
with `could not find a valid modelname` at a binned `nfet_03v3` instance
(`W=300u L=6u nf=12`), while a single-device control (`W=10u L=1u`) worked.
The cause is **not established**; treat the following as an investigation
aid, not a diagnosis. Equal PDK commits, equal library hashes and an equal
ngspice major version do not by themselves show the two executions are
equivalent.

1. Run with `options.keep_artifacts` locally and on `batch`; diff the
   generated per-corner decks (`artifacts.deck`) and logs.
2. Compare how every `.include`/`.lib` resolved: the expanded paths and
   section names in each deck/log, and their order relative to the netlist.
3. Compare the model inputs actually present on the job instance (the
   staged closure under `options.stage_model_inputs`, or the image's PDK
   tree) rather than the commit the request names.
4. Compare simulator build identity (`environment.engine_version` plus the
   full ngspice banner in the log) and any compatibility/init settings
   (`.spiceinit`, `ngbehavior`) each side applied.
5. Bisect the failing instance against the control: same deck with the
   device at `W=300u L=6u nf=12` vs `W=10u L=1u`, to see which bin the
   model selection depends on.

Only a reproducible difference from these steps justifies a simulator or
model change; none is made on the strength of the original report.

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

## Pre-composed decks (`decks` requests)

`klt sim`'s off-host backends are the sanctioned route to off-host
simulation capacity, but the corner contract above has klt *compose* each
per-corner deck itself from `corners.*`, `analysis` and `measurements[]`.
A verification harness that already renders its own decks (PDK `.lib`
section selection, derived `.meas` expressions, per-point `.param`s,
`.save` retention lists) and mints its own evidence from the raw logs needs
the **transport, not the corner engine**: re-expressing the campaign as a
corner request would mint a *different* artifact from the one its existing
evidence was minted by ([#2804](https://github.com/2AMLogic/klayout-tools/issues/2804),
re-filing #2464).

A request carrying a **`decks` list** is a deck-list request: klt runs each
deck verbatim with `ngspice -b`, in an isolated directory, and returns each
unit's raw stdout/stderr and declared output files. There is no corner
expansion, no deck composition, no measurement parsing, and no pass/fail
grading of the circuit — the caller diffs its own evidence. All four
backends accept deck-list requests (`local`, `local-parallel`, `remote`,
`batch`); the off-host ones push the deck closure and run the *same* deck
runner on the worker (`klt sim … --backend local-parallel` there lands back
in the deck path), exactly as corner jobs do.

```json
{
  "backend": "batch",
  "models": { "pdk": "sky130A" },
  "decks": [
    {
      "id": "tt_27c_3v30",
      "deck": "rendered/tt_27c_3v30.spice",
      "inputs": ["dut.spice"],
      "outputs": ["out/measure.raw"],
      "timeout_s": 3600
    }
  ],
  "options": { "max_workers": 4 }
}
```

### Request fields

| Field | Type | Description |
| ----- | ---- | ----------- |
| `decks[]` | array\<object\>, required | The units to run. Each entry: `id`, `deck`, optional `inputs[]`, `outputs[]`, `timeout_s`. Every rejectable condition below fails before any upload, SSH push, or fleet launch (exit 1). |
| `decks[].id` | string, required | Unique unit identifier; doubles as the unit's directory name under `outdir`, so it must be a single path segment (no `/`, no `..`). |
| `decks[].deck` | string, required | Path to the pre-composed deck. Relative paths resolve against the request file's directory. **Shipped byte-for-byte** — never rewritten. |
| `decks[].inputs[]` | array\<string \| {path, name}\> | Extra input files copied into the unit directory. A bare string lands at its basename; the object form names the destination explicitly. Destinations must be relative and traversal-free. |
| `decks[].outputs[]` | array\<string\> | Unit-relative paths the deck is expected to write (parent directories are created up front). After the run each is reported as present (`outputs`) or absent (`missing_outputs`) — a missing output is evidence, never a silent drop. Outputs must not name a staged input. |
| `decks[].timeout_s` | number | Per-unit wall-clock budget. Defaults to `options.timeout_s`, then `120` (the corner default). Exceeding it kills the engine; whatever it emitted first is kept. |
| `engine` | string | Must be `"ngspice"` (the default). Deck transport composes and grades nothing, so there is no `xyce` deck path. |
| `models.pdk` / `models.pdk_root` | string | Image selection for `remote`/`batch` — the same field, semantics, and validation as corner requests (`remote` refuses to provision without `models.pdk`; `batch` validates it against the published fleet images). Independent of any corner axis; PDK model files stay on the image. |
| `options.timeout_s` | number | Default per-unit timeout (see `decks[].timeout_s`). |
| `options.max_workers` | integer | Worker-pool size for `local-parallel`; overridable with `--max-workers`, capped by `$KLT_SIM_MAX_WORKERS`, same as corners. |
| `options.ngspice_binary` | string | Same resolution rules as corners (issue #2423). Interpreted on the *executing* host. |
| `remote.*` / `batch.*` | object | The same backend blocks corner requests carry; only read for the matching backend. `remote.hosts` sharding is refused for deck requests — one off-host job runs the whole list across its own worker pool. |

A deck-list request must not also declare `netlist`, `corners`, `analysis`,
`analysis_steps`, `measurements`, `monte_carlo`, `exclude`, or
`netlist_source` — a document declaring both is ambiguous about what
should run, so it is rejected by name.

The corner-only CLI knobs are likewise refused for deck requests:
`--hosts`, `--resume`, `--budget-s`, `--plot`, `--fail-fast-probe`, and
`--fail-on-diagnostic`.

### Preservation and isolation

- **Deck bytes are preserved.** Unlike the corner path — whose stager
  rewrites `.include` directives to flat staged names — a deck ships
  byte-for-byte (the unit's `deck_sha256` hashes exactly what the engine
  read). The deck's `.include`/`.inc` closure — and the file of every
  `.lib <file> <section>` model reference — is staged instead at each
  target's *declared relative path* inside the unit directory, so the
  unmodified directives resolve there. A target that cannot be staged that
  way — an absolute path outside the PDK roots, a `..`-climbing relative,
  a file that resolves nowhere on the submitting host — is a named error
  before any billable launch. `$VAR`-bearing and PDK-rooted targets stay
  the executing host's to resolve, exactly as in the corner path (and are
  listed in the unit's `host_resolved`; the executing host expands `$VAR`/`~`
  in them before running the engine). Staged files are followed
  recursively, bounded by the same 64-file / 32 MiB caps as corner
  staging. A one-token `.lib <section>` … `.endl` *definition* names no
  file and is left alone; any other `.lib` shape (no arguments, more than
  two, unbalanced quotes) is rejected by name.
- **Units are isolated.** Each unit runs in its own directory
  (`<outdir>/<id>/`), recreated fresh every run. Its deck, declared
  inputs, and staged closure are copied there under the declared names, so
  two units may share filenames freely. `stdout.bin` and `stderr.bin` at
  the unit root are reserved for the captured streams: no deck, input,
  staged include, or declared output may claim them. `ngspice -b <deck>`
  runs with the unit directory as `cwd` — the invocation a local `ngspice -b` in the
  caller's own harness would be.
- **Streams are evidence, untouched.** Each unit's engine stdout and
  stderr are captured as separate **binary** files (`stdout.bin`,
  `stderr.bin`) with no decoding, stripping, or normalization.
  Byte-for-byte means preserving what the worker's engine emitted:
  equality with a separate local run requires matching engine build,
  inputs, and environment — paths, banners, and timing can differ. klt
  never normalizes logs to manufacture equality.

### Response

The report carries `"mode": "decks"` so a consumer can tell it from a
corner report at a glance. `schema_version` is `klt sim`'s own (versioned
per command, see [`docs/json-contract.md`](../json-contract.md)); the deck
report is the additive `mode: "decks"` shape under that version. Stream
and output paths are **relative to `outdir`** (which is absolute on the
machine that assembled the report — the caller's, after an off-host run's
collection), so a unit's evidence is `<outdir>/<id>/stdout.bin` and so
on, under any backend.

Every `<outdir>/<id>/` reflects only the current run, on every backend.
`outdir` is reused across runs (the default `.klt/sim` always is), so an
off-host run never merges into it: each unit directory is cleared, then
replaced by the tree this run collected from the instance or the fleet.
A unit whose tree did not come back has no directory, and its declared
outputs are listed in `missing_outputs`. A previous run's files are never
reported as this run's evidence. Content of `outdir` that is not a unit
directory of the request is left alone.

| Field | Description |
| ----- | ----------- |
| `mode` | `"decks"` |
| `status` | `"pass"` when every unit exited 0 within its timeout; `"error"` otherwise. klt never grades the deck's electrical result. |
| `unit_count` / `passed` / `errored` / `timed_out` | Counts over `units[]`. |
| `units[]` | One entry per deck, in request order: `id`, `status` (`"pass"` / `"error"` / `"timeout"`), `exit_code` (`null` when the engine never launched), `timed_out`, `duration_s`, `engine_version` (best-effort from the engine's own stdout banner — recorded per unit and top-level as engine identity), `deck` (unit-relative name as run), `deck_sha256`, `stdout`, `stderr`, `outputs`, `missing_outputs`, `host_resolved` (include targets left to the executing host), `diagnostics[]` (`timeout`, `engine_exit`, `engine_not_found`; off-host job failures carry `batch_job_failed` / `batch_job_timeout` / `batch_poll_timeout`, with the job's own klt error envelope attached where one was recoverable, or `remote_job_failed` when the `remote` backend's SSH run fails — the unit trees already written on the instance are pulled back before teardown, so `stdout`/`stderr`/`outputs` point at whatever evidence was recovered and the message says which streams came back). |
| `outdir` | Absolute path of the artifacts directory on the reporting host. |
| `environment` | `engine`, `engine_version`, `ngspice_binary`, `engine_host` (`"local"`/`"worker"`; off-host runs also `client_ngspice_binary`, and `ngspice_binary` is then the worker's, see "Which machine ran it"), `klt_version`, `max_workers`; off-host runs add `remote` — the same block corner reports carry (`provider`, `instance_id`/`instance_type`, `job_id`/`bucket` for batch, `runner_klt_version` identity, ...). |

### Runner support for `decks[]` on the batch backend (issue #3303)

A fleet runner image baked from a `klt` that predates `decks[]` reads a
deck-list request as a corner request and fails with `request is missing
required field: netlist`. The major.minor version gate cannot see this (the
capability arrived mid-`0.7` line), so a deck-list batch submit has its own
check, which `runner_version_check: "warn"` does **not** relax (the job could
not run either way):

- **Before submit** — if the AMI manifest publishes a `klt_version` for every
  image the request could boot and none of them knows `decks[]` (`0.7.0` or
  earlier, or `0.7.1.devN` with `N` < 307; `0.7.1` and later releases do),
  the request is refused with `error.code` `batch_runner_lacks_decks`: no S3
  write, no launch. Corner requests are unaffected.
- **On the runner** — when the manifest cannot say (no manifest, a legacy
  entry without `klt_version`, a mix of images), the job's preflight applies
  the same rule to the runner's own `klt --version` and, if it lacks `decks[]`
  (or the version is unreadable), exits 87 before `klt sim` with a klt error
  envelope carrying `batch_runner_lacks_decks`. The job is still launched, but
  fails in seconds with the named cause instead of a corner-request message.
  Chosen over refusing outright so a legacy manifest does not block a capable
  runner.
- **Recovery** — a failed deck job whose envelope is the corner-style
  `missing required field: netlist` gets a note on every unit's diagnostic
  that the runner's `klt` predates `decks[]`.

Rebuilding the fleet runner image from a `klt` that includes `decks[]` is a
worker/fleet-spec change outside this repository.

### Exit codes (deck mode)

| Code | Meaning |
| ---- | ------- |
| `0` | Every unit ran to completion with engine exit 0 (`status: "pass"`). |
| `1` | Failed to run at all — bad deck list, unstageable include, unsupported engine/image selection, backend failure before or during dispatch. |
| `2` | Usage error (argparse), as always. |
| `4` | At least one unit errored or timed out (`status: "error"`) — the run is incomplete. Per-unit exit codes, streams, and missing-output lists say which and why. |

Exit `3` cannot arise: deck requests carry no graded measurements. Gate on
the payload's `status` field, not the exit code.

**Worked shape:** render your decks, list them, submit. A paired
re-take campaign points `models.pdk` at the same image the original record
used and diffs the returned `stdout.bin`/`outputs` against its own
evidence — the artifact is the one its harness mints, not one klt
re-composed.

## Sequential (adaptive) campaigns on the batch backend

`corners`/`monte_carlo` batch a grid that is known up front. When each
probe's input depends on the previous probe's result (bisection for a
threshold or boundary), the full request cannot be written ahead of time.
The intended pattern is an external controller that submits **one
single-unit request per step** and reads each report before choosing the
next input. There is no `--wait` flag, adaptive request kind, or resident
worker to add: `--backend batch` already submits, launches, polls, and
collects before it returns.

```sh
klt sim probe-000.request.json --backend batch --format json > probe-000.report.json
```

or, from Python, `klayout_tools.sim.run_sim(request_path, backend="batch")`.
The library call returns the report dictionary (and raises `SimError` where
the CLI would emit an error and exit `1`); the CLI writes that same report
to stdout and maps `status` to an exit code (see "Exit codes"). A driver must
parse the report, not gate on the exit code alone: exit `3` (a declared limit
failed) still carries completed, usable measurements, and may be a perfectly
good bracket observation, while exit `4` (`error`, `inconclusive`,
`not_checked`) must stop the campaign.

A single-unit request uses one process, one temperature, and a singleton
value for a literal DC source, with `monte_carlo` omitted:

```json
"corners": { "process": ["tt"], "supply_v": { "vdd": [1.05] }, "temperature_c": [27] }
```

[`examples/sim-batch/adaptive-search.py`](../../examples/sim-batch/adaptive-search.py)
is a bounded bisection driver built on this (usage in
[`examples/sim-batch/README.md`](../../examples/sim-batch/README.md)). The
controller runs on the submitting host; every simulation runs on the fleet,
via `klt sim --backend batch` only. Before moving the bracket it requires
exactly one corner, a usable report and corner `status` (`pass` or `fail`),
the expected source value, and a present, finite named measurement with a
usable status and a batch job id not seen before. Anything else (an error
envelope, `error`/`inconclusive`/`not_checked`/`pass_partial`, a missing or
non-finite value, an unexpected corner count, a repeated job id) stops the
campaign: a missing value is never replaced by zero. It also stops when
`--max-probes` is spent.

Caveats:

- **Assumptions.** The example assumes the measurement is monotonic in the
  source value over the bracket and that the bracket is valid (the crossing
  lies inside it); the endpoints are not probed.
- **Overhead.** Each probe is a separate fleet job: it pays launch,
  Spot-acquisition, and boot latency every time, and there is no resident
  worker or shared simulator state between probes. Prefer a fixed grid when
  the points are known in advance.
- **Timeouts.** `batch.poll_timeout_s` expiring yields a `batch_poll_timeout`
  diagnostic and does not prove the job stopped. The driver stops and reports
  the job id (`environment.remote.job_id`) instead of starting a replacement.
  Bound a probe with `batch.poll_timeout_s` and `batch.capacity_wait_s`
  (above). The example performs no automatic campaign retries.
- **Scope.** Arbitrary `.param`/`.nodeset` axes are not covered, and the
  example does not negotiate features with, or repair, an incompatible fleet
  image.

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
  launch, or collect is a `lost_shard` exactly as above (a capacity refusal
  included, once that shard's own `batch.capacity_wait_s` budget is spent); a shard whose job
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

  On `remote` and `batch`, `--budget-s` is forwarded to the worker by writing
  it into the pushed request's `options.wall_clock_budget_s` (the CLI value
  wins over the request's own), so the job's `local-parallel` loop enforces
  it ([#3167](https://github.com/2AMLogic/klayout-tools/issues/3167)). On a
  `hosts > 1` sharded run every shard's worker applies the full budget from
  its own start, so the whole-run bound is approximately, not exactly, the
  budget.

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
  file is removed once a sweep finishes with nothing skipped. Implemented for
  `local`/`local-parallel` (including any `hosts > 1` shard on them) and
  `batch` (recorded after the job returns; corners that never ran --
  `lost_shard`, `batch_job_failed`, `batch_job_timeout`, `batch_poll_timeout` --
  are not checkpointed, so only they are re-submitted) — pairing `options.resume` with `backend: "remote"` is a
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

## Timeout-budget hint

Issue [#2894](https://github.com/2AMLogic/klayout-tools/issues/2894): when
any unit carries a `timeout` diagnostic, or ran for at least 80% of
`options.timeout_s`, the report gains an additive, optional
`environment.timeout_budget_hint` object (absent otherwise; no existing
field changes):

```json
{"max_unit_runtime_s": 490.2, "timeout_s": 600, "suggested_timeout_s": 1200,
 "units_timed_out": 2, "message": "..."}
```

It states that the failure is a per-unit time budget, not a netlist problem,
and suggests a larger `options.timeout_s` (twice the larger of the two
numbers, rounded up to a whole minute).

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
request's verbatim `.meas` cards, and a `.control` block that `pre_osdi`-loads
any declared OSDI libraries, restores the full saved set with `save all` (see
"Saved signal set" below), applies any `corners.params` row (`alterparam` + `reset`), `alter`s the
supply sources, and runs the declared analysis. A netlist that already
carries its own `.end` (a full deck exported as-is, e.g. straight off some
schematic tools) is not supported in this version — strip the top-level
control/end cards before pointing a request at it. A PDK whose devices are
Verilog-A compact models does **not** need a `.control` block in the body
either — declare the compiled `.osdi` files in `options.osdi_preload` instead
(see "OSDI (Verilog-A) model preload" below).

Because `klt sim` owns that single `.control` block, a measurement that has no
top-level `.meas` form — most notably an operating-point quantity — is
declared as a `measurements[].expr` expression rather than smuggled in through
the netlist body; see "Measurements that are not `.meas` cards" below.

## Saved signal set (`save all`)

ngspice's `.save` card **restricts** the batch run's saved node/branch set —
whatever the netlist's own last `.save` names, and nothing else, is what
survives to be read by a `.meas` card or a rawfile capture. Schematic
netlisters emit one routinely (e.g. narrowing the saved set to a supply-current
probe), and a circuit body carrying one used to silently starve every other
`.meas`/`options.waveforms` request this command generated for it — both a
genuinely missing measurement and one merely excluded by the body's own
`.save` card surfaced identically as `code: "measurement"`.

`klt sim` now emits its own `save all` inside the generated `.control` block
whenever the request declares any `measurements[]` or `options.waveforms` —
after the `.include <netlist>` line so it wins over (ngspice applies `save`
cards in the order encountered) any `.save` the netlist body itself carries,
restoring the full saved set regardless, and before the `alter` cards and the
analysis command that read it. Only `options.osdi_preload`'s `pre_osdi` lines
(see "OSDI (Verilog-A) model preload" below) precede it in the block, and they
cannot interact with it: a `pre_`-prefixed command is hoisted out and executed
before the circuit is even parsed. A request declaring neither `measurements[]`
nor `options.waveforms` omits `save all` entirely, leaving that path
byte-identical to before (issue #2521).

### Solver options: `options.ngspice_options` (issue #3111)

`options.ngspice_options` declares ngspice solver/convergence `.options` for
the request, so the setting is part of the report's evidence instead of being
buried in a netlist include:

```json
{ "options": { "ngspice_options": { "gminsteps": 0, "itl1": 400, "noopiter": true } } }
```

klt renders one `.options gminsteps=0 itl1=400 noopiter` card after `.temp` in
each corner deck (alongside a Monte Carlo `.options seed=`, which is
unchanged) and reports the map as `environment.ngspice_options`. `true`
renders the bare flag; `false` renders nothing (the ngspice default) but is
still echoed. The map is request-wide: split requests to use different
settings per device family. Only the solver options listed in the table below
are accepted, because ngspice silently ignores an unknown `.options` name.

This is distinct from `options.ngspice_init`: `.spiceinit` lines are not
reliably honoured for `.options` names (a `.spiceinit` `option itl1=1` had no
effect on the solve, the same name as a deck `.options` did), so
`ngspice_init` cannot carry them.

For `remote`/`batch` the map rides the forwarded request document. A worker
that predates the field would drop it silently, so the client compares the
returned report's `environment.ngspice_options` with the declared map and
fails with an application error naming the option on any difference. The
map is also part of the `options.resume` checkpoint fingerprint.

### Opting out: `options.save_mode` (issue #2732)

`options.save_mode` decides who owns the corner's saved vector set. It takes
exactly two values:

| Value       | Effect |
| ----------- | ------ |
| `"all"`     | The default (also what omitting the key means). The generated `save all` above is emitted under exactly the conditions and in exactly the position described above — a request that omits the key produces a byte-identical deck. |
| `"netlist"` | No generated `save all`, in either the real corner deck or the `options.fail_fast_probe` calibration deck. The included netlist's own `.save` cards (or ngspice's own default when it carries none) decide what is resident. |

Anything else — `null`, a boolean, an array, an unknown or differently-cased
string — is an application error (exit 1) before any corner, probe, remote
instance or batch job is launched. An explicit `save_mode` (either value) with
`engine: "xyce"` is refused the same way: the Xyce deck has no `.control`
block and no generated save card to control. Omitting it leaves Xyce requests
unchanged.

`"netlist"` is the opt-out for large transient decks where keeping every node
and branch resident is the cost you want to avoid. `klt sim` does **not**
parse, rewrite, infer or widen the netlist's `.save` selectors: under
`"netlist"`, **the caller owns the vector coverage** every `measurements[]`
entry (`spice` or `expr`) and every `options.waveforms` consumer needs. A
measurement that reads an excluded vector is not silently zero — it comes back
`value: null`, `status: "error"` with the usual `code: "measurement"`
diagnostic (and usually `code: "no_such_vector"` too), whose message names the
`"netlist"` mode and how to fix it; the corner and the run grade `error`, never
`pass`. The waveform artifact likewise contains only the retained vectors.
No speedup or memory figure is claimed here; measure it on your own circuit.

A complete request against a netlist body that keeps only `v(out)`:

```spice
* body.spice
Vin in 0 DC 1
R1 in out 1k
R2 out 0 1k
.save v(out)
```

```json
{
  "netlist": "body.spice",
  "analysis": {"kind": "tran", "args": "1u 10u"},
  "measurements": [
    {"name": "vout", "spice": ".meas tran vout FIND v(out) AT=5u",
     "unit": "V", "limits": {"min": 0.45, "max": 0.55}},
    {"name": "vout_pk", "expr": "vecmax(v(out))", "unit": "V"}
  ],
  "options": {"save_mode": "netlist", "waveforms": true}
}
```

Both measurements resolve and the waveform carries `time` and `v(out)` but
not `v(in)`; adding a measurement on `v(in)` would report it as an error.
Without `save_mode` (or with `"all"`), `v(in)` is retained as before.

The option is honored by `local`, `local-parallel`, `hosts > 1` local shards
and the fail-fast probe. For `remote`/`batch` it rides the forwarded request
document (single job and every shard alike) and is applied by the worker's
own `klt sim` — so **the worker image must include this implementation**. An
older worker that predates the field does not know it and keeps emitting
`save all`; worker-version compatibility diagnostics are tracked by
#2733/#2719. Under `resume`, a checkpoint written under one mode is never
reused for the other.

### Restricting the saved set: `options.save_vectors` (issue #3211)

`options.save_vectors` is a non-empty array of ngspice vector names (e.g.
`["v(out)", "i(vdd)"]`; no whitespace inside an entry, names are folded to
lower case). When set, the generated `save all` is replaced by a single
`save <vectors>` card, emitted in the same position and under the same
conditions, so a large transient keeps only the signals you named resident
(and in the rawfile/waveform artifact) without editing the netlist.

klt always adds every `v(x)`/`i(x)` (and derivative forms such as `vdb(x)`,
`vm(a,b)`) referenced by a declared `measurements[].spice`, `measurements[].expr`
or `crossings.signal`, so measurements stay correct. A measurement that names
a vector *bare* (`out` rather than `v(out)`) is not detected — list it in
`save_vectors` yourself. `save_vectors` and `save_mode: "netlist"` are mutually
exclusive (an application error: the latter already suppresses the card the
former rewrites), and `engine: "xyce"` refuses it like `save_mode`. Omitting it
leaves the deck byte-identical. A changed `save_vectors` invalidates a
`--resume` checkpoint. It rides the forwarded request document to
`remote`/`batch` workers, so the worker image must include this implementation.

### Waveform size guard: `options.waveform_max_bytes` (issue #3211)

Waveforms are written as an ASCII rawfile (`waveform.raw`) and then converted,
fully in memory, to `waveform.raw.json`. A long, finely-stepped transient can
produce a multi-gigabyte rawfile whose conversion is OOM-killed. When
`waveform.raw` is larger than `options.waveform_max_bytes` (a positive integer
byte count; default `1073741824`, 1 GiB; `null` disables the guard), the
conversion is skipped and the corner carries a `severity: "warning"`,
`code: "waveform_too_large"` diagnostic naming the size, the limit and the
remedies. The raw file stays at `artifacts.raw`, `artifacts.waveform` is
`null`, and every measurement is graded as usual — the warning does not change
`status`. Ways to shrink a waveform: `options.save_vectors`, a larger `tstep` /
`tmax`, a shorter `tstop`. Validated before any corner launches; honored by
every backend (remote/batch workers re-read it from the request document).

### `tran` timing arguments (`tstep tstop tstart tmax`)

`analysis.args` is passed to ngspice verbatim, so the full four-token form
`"args": "100p 4u 0 25p"` (`.tran tstep tstop tstart tmax`) is supported:
`tstart` delays data recording and `tmax` caps the internal timestep (a
smaller `tmax` makes the rawfile larger; a larger `tstep` makes it smaller).
There is no separate `analysis.tmax` field.

**Diagnosing a still-missing measurement.** When a `.meas`/rawfile signal
still comes back empty after this fix, ngspice's log gives the same `Error:
no such vector as <name>.` line (classified as `code: "no_such_vector"`,
alongside the generic `code: "measurement"`) whether the name is a genuine
typo/dead node or — pre-#2521, or on an `options.save_mode: "netlist"`
corner — a vector that exists but was excluded from the saved set. Verified
empirically against ngspice 46: there is no log-side signal that tells the
two apart, so `no_such_vector` names the *symptom* ("nothing resolved for
this name"), not the cause; treat it as "check the signal name against the
netlist body, including any `.save` card it carries" rather than a
self-diagnosing error.

## Measurements that are not `.meas` cards (`measurements[].expr`)

A `measurements[]` entry declares **exactly one** of two forms:

| Form    | What it is                                                          | Where it lands in the deck                    |
| ------- | ------------------------------------------------------------------- | --------------------------------------------- |
| `spice` | A verbatim top-level `.meas` card                                   | File scope, before the `.control` block       |
| `expr`  | A single-line ngspice **expression**, evaluated after the analysis  | Inside `klt sim`'s own `.control` block       |

Declaring both, or neither, is an application error (exit 1).

Under a scalar `analysis.kind: "op"`, a `spice` card is refused at request time
(exit 1): an operating point has no dc/ac/tran sweep for a `.meas` card to
search. Use `expr` (for example `v(out)`); no dummy `dc` sweep is needed for
operating-point figures. Other kind/card mismatches are not checked here.

### Why `expr` exists

A top-level `.meas` card is the only measurement shape ngspice's batch mode
accepts at file scope, and ngspice's `.MEASURE` implements no `op` analysis
type at all (see `measurements[]` in the request table, and the `.meas op`
rejection above). The "Netlist convention" section above also gives `klt sim`
sole ownership of the deck's single `.control` block — the caller cannot
inject one from either side. Together, those two facts meant that **a
quantity which is not a single `.meas` card's search had no expression in a
request whatsoever**
([#2533](https://github.com/2AMLogic/klayout-tools/issues/2533)):

- an **operating-point** quantity (`analysis.kind: "op"`) — no `.MEASURE OP`
  exists, so there was no card to write;
- a **reduction or combination** of saved vectors — peak-to-peak swing,
  a ratio of two nodes, a derived figure of merit — which needs arithmetic
  across a vector or across two `.meas`-shaped searches that no single card
  performs.

A block whose ratified spec rows are of either kind could not produce a `klt
sim` envelope covering its spec at all — and design-signoff tier-1 item 5
("full corner verification vs a ratified spec") accepts only a `sim` envelope
for an analog partition, so citing the `.meas`-expressible subset would have
graded the item `met` on evidence narrower than the spec it names. `expr`
closes that for any spec row derivable from a single corner's single solve;
see [`signoff.md`](signoff.md), and "Several solves per corner
(`analysis_steps[]`)" below for figures defined across more than one solve.

### What `klt sim` generates

For each `expr` entry, in **declared order**, inside the generated `.control`
block:

```
let <name> = <expr>
print <name>
```

Emitted **after** the analysis command (the expression reads the vectors the
analysis produced) and **after** the optional rawfile `write` — deliberately:
`let` adds its result to the current plot and `write` dumps every vector in
that plot, so evaluating earlier would silently add derived vectors to the
`options.waveforms` artifact. Declaring an `expr` never changes what the
rawfile/waveform artifact contains.

A `print`ed **scalar** lands in the log as `<name> = <value>` — the same shape
a successful `.meas` result uses — so it is harvested, unit-stamped, and
graded against `limits`/`plausible_range` exactly like a `.meas` measurement,
and it participates in `monte_carlo` statistics and `coverage` identically.
There is no separate response shape and no `schema_version` bump: an `expr`
measurement is an ordinary `corners[].measurements[]` entry.

Because the `let` lines are emitted in declaration order into one plot, a
later expression may reference an earlier one's name:

```json
"measurements": [
  { "name": "vmid",    "expr": "v(mid)",      "unit": "V"  },
  { "name": "vmid_mv", "expr": "vmid * 1e3",  "unit": "mV" }
]
```

### `expr` after a `noise` analysis

ngspice leaves the *integrated* plot current after `noise` (`noise2`:
`onoise_total`, `inoise_total`); the spectra (`inoise_spectrum`,
`onoise_spectrum`) live in the first plot, `noise1`. For a single `noise`
analysis with at least one `expr` measurement, `klt sim` therefore emits
`let inoise_spectrum = noise1.inoise_spectrum` (and the same for
`onoise_spectrum`) before the `expr` lets. Both the totals and the spectra
resolve by **bare name** (e.g. `onoise_spectrum[10]`, `onoise_total`); the
`noise1.` prefix also still works. The aliases come after any rawfile `write`,
so `options.waveforms` output is unchanged, and decks for other analysis kinds
are byte-identical.

Inside `analysis_steps`, `$curplot` after a `noise` step is likewise the
totals plot, so a step-scoped `expr` sees `onoise_total`/`inoise_total` only.
A derived (top-level) or later-step `expr` reaches a `noise` step's spectra
with the `<step>.spectrum.<vector>` reference, where `<vector>` is
`inoise_spectrum` or `onoise_spectrum`:

```json
{ "name": "a_spot", "expr": "a.spectrum.onoise_spectrum[10]", "unit": "V/sqrt(Hz)" }
```

The spectrum plot name is captured per step (it is not a fixed `noise1`), so two
`noise` steps resolve to their own distinct plots. The capture is emitted only
for `noise` steps that a `.spectrum.` reference names, so every other deck is
byte-identical. `<step>.spectrum.<vector>` on a non-`noise` step, an unknown
vector, or a forward/unknown step fails validation naming the measurement and
step. If the `noise` step fails, its dependents are reported
`derived_input_unavailable` (alongside the step's `step_failed`), never a stale
value. A step-scoped measurement that is literally named `spectrum` is still
referenced as `<step>.spectrum`; only the three-part form is the qualifier.

### Noise units and `sqrnoise`

`measurements[].unit` is a caller-supplied label; klt never checks it against
the values. ngspice's `sqrnoise` option (from a `.spiceinit`, a worker image
default, or `options.ngspice_init`) squares every noise output vector, so the
same request can yield amplitude (V, A) or power (V², A²) numbers that both
grade `pass`. A `noise` corner whose rawfile was parsed carries
`noise_representation` (`"amplitude"` or `"power"`, see the report fields
table) so callers can check mechanically. To pin amplitude regardless of the
environment, set `options.ngspice_init: ["unset sqrnoise"]`.

### `expr` cannot read `.meas` results

The chaining rule above covers **`expr` -> `expr`** only. A `spice` entry's
`.meas` card is a file-scope card that ngspice evaluates itself; its scalar is
printed to the log but is **not** a vector the generated `let` can see. An
`expr` that names a `.meas` result grades the corner `error` with the
`measurement` diagnostic "printed no '<name> = <value>' line" (the engine log
shows `vector <name> is not available or has zero length`). This applies to
ordinary file-scope `.meas` cards; `analysis_steps[]` control-scope `meas`
has different semantics (see that section). Making `.meas` scalars visible to
`expr` is a separate contract change.

```json
"measurements": [
  { "name": "ta", "spice": ".meas tran ta when v(o)=0.8 rise=3" },
  { "name": "tb", "spice": ".meas tran tb when v(o)=0.8 rise=11" },
  { "name": "t",  "expr": "(tb - ta)/8" }
]
```

The `t` entry above errors. Note that grading `tb - ta` instead does not help:
that is still an `expr` over two `.meas` results and fails the same way. The
working fix is a single **trig/targ** `.meas` card that measures the interval
directly, with the bounds scaled to match:

```json
"measurements": [
  { "name": "p8", "spice": ".meas tran p8 trig v(o) val=0.8 rise=3 targ v(o) val=0.8 rise=11", "unit": "s" }
]
```

`p8` is eight periods, so bound it at 8x the per-period limits (on ngspice-46
a 1 MHz sine gives `p8 = 8.00000e-06`). A constant offset or unit conversion
works the same way: rather than grading `180 + ph*180/pi` as an `expr`, grade
the raw `.meas` result `ph` against bounds converted back into its own unit
(radians). Other options are to derive the figure outside `klt sim` from the
reported raw measurements, or to compute it directly from vectors in `expr`
(a `vecmax()`/`mean()` reduction that does not reference any `.meas` result).

### A `spice` card cannot read another `.meas` result

The same limit applies **`spice` -> `spice`**. A card whose value field
(`at=`, `from=`, `to=`, `td=`, `val=`, ...) names another measurement's result:

```json
"measurements": [
  { "name": "t_flip", "spice": ".meas tran t_flip when v(dn)=0.5 rise=1 td=8u" },
  { "name": "vos",    "spice": ".meas tran vos find v(rmp) at=t_flip" }
]
```

fails inside ngspice with `Undefined parameter [t_flip]`, because a file-scope
`.meas` card resolves that right-hand side as a netlist `.param`. The whole
unit would then read `produced no value`. `klt sim` instead refuses the
request up front (exit 1, standard error envelope) naming the dependent card
and the measurement it references. Only an identifier directly after `=` that
equals another `measurements[]` name (case-insensitive) is flagged; `v(vout)`
or a card's own name are not. A real netlist `.param` that shares a
measurement name is refused as well. `expr` entries and `analysis_steps[]`
measurements are not scanned.

Workarounds: fold the pair into one self-contained card
(`.meas tran vos find v(rmp) when v(dn)=0.5 rise=1 td=8u`), measure an
interval with a single **trig/targ** card, or compute the figure with an
`expr` over saved vectors.

### `.meas` `WHEN` thresholds must be literal numbers

ngspice's `.meas ... when v(x)=<value>` accepts only a literal number (or a
`.param`-style parameter reference) on the right-hand side, not an arithmetic
expression. Verified on ngspice-46:

```
.meas tran good when v(o)=0.81        ; works
.meas tran bad  when v(o)=0.5*1.62    ; fails
```

The failing card logs `Error: measure  bad  when(WHEN) : Expressions like
0.5*1.62 are not supported.` and no value is printed. `klt sim` does not lint
card text (parameterized cards are valid); instead, when the log names the
failing measurement, that error text is appended to its `measurement`
diagnostic (`measurement 'bad' produced no value -- ngspice reported:
Expressions like 0.5*1.62 are not supported.`), bounded in length and
available without `keep_artifacts`. An engine `.meas` error that cannot be
matched to a missing measurement is reported once as a corner-level
`measurement` diagnostic with severity `warning`; grading is unchanged.

`FIND ... AT=` does not accept an arithmetic expression either. On ngspice-46,
`at=0.5*2.5u` fails with the misleading `find(AT) : out of interval`. Use a
literal time or a `.param` reference. This error follows the same
`Error: measure <name> ...` log format, so it is attached to the measurement's
diagnostic in the same way.

### `FIND ... AT=` at a `dc` sweep's end point

ngspice interpolates `AT=` inside the swept axis. When the sweep step is not
exactly representable in binary, the last sample accumulates rounding error and
can land just short of the literal end point, so `AT=<hi>` fails with
`out of interval` even though the interior points resolve. Verified on
ngspice-46 through `klt sim` with `v(out)` on a 1k/1k divider:

```
dc args "V1 0 1 0.25"   AT=0 -> 0, AT=0.5 -> 0.25, AT=1 -> 0.5   ; works
dc args "V1 0 1 0.1"    AT=0 -> 0, AT=0.5 -> 0.25, AT=1 -> error  ; out of interval
```

The failing measurement carries ngspice's text (`measurement 'hi' produced no
value -- ngspice reported: out of interval`). `klt sim` does not rewrite the
card. Either pick a step that lands exactly on the end point (a power-of-two
fraction), or read the last sample from `options.waveforms` instead of using
`AT=`.

### Operating-point values via `spice` cards

For a runner without `expr`, read an operating-point quantity with a `spice`
`.meas dc` card. A **one-point** `dc` sweep (`dc args "V1 0 0 1"`) fails:
`measure <name> find(AT) : out of interval`, because `AT=` needs two samples
to interpolate between. Sweep a dummy source over **two points** and read at
the first one:

```
V1 in 0 1
Vdummy dmy 0 0
Rdmy dmy 0 1k
R1 in out 1k
R2 out 0 1k
dc args "Vdummy 0 1 1"
.meas dc vout_op find v(out) at=0
```

A source that nothing else uses is a harmless sweep axis, so every point
carries the same operating-point value. (The one-point failure is as
reported against ngspice-46 on the fleet; this recipe was not re-run
through `klt sim` when written.) Declare the card's name in lower
case (see above), and expect the error text on a one-point sweep to be
attached to the measurement's `measurement` diagnostic.

### The expression must reduce to a single real scalar

This is the one contract rule the tool cannot check up front. ngspice's
`print` renders a **multi-point** vector (or a complex `ac`/`sp` result) as an
indexed table rather than a `<name> = <value>` line, so nothing is harvested
and the measurement grades `status: "error"` with a `code: "measurement"`
diagnostic that names both causes. Reduce the expression yourself —
`vecmax()`, `vecmin()`, `mean()`, `db()`, `mag()`, `<expr>[0]` — rather than
relying on `print`:

```json
{ "name": "swing", "expr": "vecmax(v(out)) - vecmin(v(out))", "unit": "V" }
```

A `print`ed scalar uses ngspice's `numdgt` digit count; see "Measurement
output precision" above for widening it via `options.ngspice_init`.

Worked `op` example — the case that had no expression at all before:

```json
{
  "netlist": "divider.spice",
  "analysis": { "kind": "op", "args": "" },
  "measurements": [
    {
      "name": "vmid",
      "expr": "v(mid)",
      "unit": "V",
      "limits": { "min": 1.9, "max": 2.1 }
    }
  ]
}
```

### Constraints and interactions

- **Name rules are stricter than for a `spice` card.** An `expr` name is
  emitted as an ngspice vector name *and* read back out of the log, so it must
  match `[A-Za-z_][A-Za-z0-9_]*` — no dots, no leading digit. A `.meas`
  card's `name` is unaffected (it is never a `let` target), so an existing
  request using e.g. `stage1.vout` keeps working.
- **Case is preserved in the response, but folded for matching.** ngspice
  lower-cases the vector name it prints (`print Vmid` → `vmid = 2.0e+00`), so
  an `expr`'s value is harvested case-insensitively; the response entry still
  carries the caller's own spelling (`"name": "Vmid"`). A `.meas` card's name
  is folded the same way
  ([#2546](https://github.com/2AMLogic/klayout-tools/issues/2546)) — ngspice
  lower-cases that one in its report too, so an upper/mixed-case `spice` name
  now harvests exactly as a matching-case one always has.
  **Runners older than that fix** (the pinned 0.5.0 fleet image) match
  case-sensitively: a `.meas` named with upper-case letters (`ibus_B`,
  `d_P_3`) runs and ngspice prints `ibus_b = ...`, but the job reports
  `measurement ... produced no value` — while the same request graded locally
  by a current client returns the value. A client/runner skew therefore hides
  the failure until the job returns. Workaround: lower-case every
  measurement name when targeting `KLT_SIM_BACKEND=batch` on such a runner.
- **An `expr` name must be unique** across the whole `measurements[]` list
  (compared case-insensitively, per the previous bullet): a second `let
  <name>` overwrites the first, so both response entries would report the same
  value. Duplicate names among `spice`-only entries are left to ngspice
  exactly as before.
- **Single line only.** The expression is spliced verbatim into one `let`
  line; an embedded newline would become a separate control command.
- **No bare `<` / `>`.** In ngspice's control language these are I/O
  redirection, not comparisons: `vecmax((v(a) < v(b))*v(a))` would redirect
  stdin/stdout, create stray files in the corner directory and report
  `produced no value`. `klt sim` rejects such an `expr` up front (application
  error, exit 1) before any ngspice launch. Spell comparisons `gt` / `lt` /
  `ge` / `le` / `eq` / `ne`: `vecmax((v(a) lt v(b))*v(a))`.
- **`corners.*` / `monte_carlo`**: nothing special. The `let`/`print` pair is
  regenerated in every corner's own deck, after that corner's `alter` cards
  and analysis, so the expression is evaluated against that corner's solve.
  Monte Carlo statistics (`monte_carlo.quantiles`, `k_sigma`, the `mean ±
  k*stddev` window) apply to an `expr` measurement's per-sample values with no
  extra declaration — the stats layer keys off `measurements[].name`, not the
  form it was declared in.
- **`engine: "xyce"` refuses `expr`** (application error, exit 1): `let` and
  `print` are ngspice control commands and the Xyce deck has no `.control`
  block (its analysis is a plain top-level dot card). Use `engine: "ngspice"`,
  or express the quantity as a `.meas` card. Refused by name rather than
  silently dropped — a corner that ran without its `expr` lines would report
  every such measurement as "produced no value", which reads as a circuit
  regression instead of an unsupported request field.
- **`remote`/`batch` backends** need no special handling: `expr` is deck text,
  not a host artifact, and the off-host worker generates the deck from the
  same request document.
- **One solve per `expr`, by default.** With the scalar `analysis`, `expr`
  reaches everything derivable from *one* corner's *one* solve. A figure
  defined across several solves in the same corner (a line/load regulation,
  a `d(out)/d(in)` sensitivity, a ratio of two DC solves) uses
  `analysis_steps[]` instead — see "Several solves per corner" below. A
  figure defined across *corners* or Monte Carlo draws is still out of
  scope.
- **`--op-lint`** scans a measurement's `v(...)` references from `expr` as
  well as from `spice`, so an operating-point measurement naming a node the
  netlist does not define is still a named finding rather than a silent 0 V
  (see "Operating-point lint" below).

## Optional measurements (`measurements[].optional`)

By default a `spice`/`expr` measurement that produces no value grades its
corner `error` (exit 4). That is right when the figure should always exist,
and wrong when its absence is itself the answer -- for example an
edge-triggered `.meas` (`TRIG/TARG`, `WHEN ... RISE=n`) on an oscillator copy
biased below start-up, which legitimately has no edges at that corner.

`"optional": true` (boolean, default `false`) declares that a value-less
result is *data, not a failure*:

```json
{ "name": "t_rise_copy2", "spice": ".meas tran t_rise_copy2 trig v(a2) val=0.5 rise=1 targ v(a2) val=0.5 rise=2", "optional": true, "unit": "s" }
```

When an optional measurement yields no value it is reported as

- `value: null`, `margin: null`, **`status: "absent"`**, and
- an `info`-severity diagnostic `code: "measurement_absent"`,

and it does **not** make the corner `error`, `fail` or `inconclusive`; the top-level `errored` count and the exit code are unchanged by it. `limits` and
`plausible_range` are not evaluated for an absent value (no pass/fail claim is
made either way). Every request that omits `optional` (or sets it `false`) is
byte-identical to before. A non-boolean `optional` is an application error
(exit 1).

**What stays an error.** `optional` only covers "the figure does not exist
here". A `.meas` card that ngspice rejects (any `Error: measure ... : <reason>`
other than `out of interval`, e.g. a malformed `WHEN` expression) still grades
`error`, as does a simulation that failed, and any *non-optional* measurement in
the same request that has no value still grades its corner `error`.
`out of interval` is ngspice's reason when a `WHEN`/`TRIG`/`TARG` crossing is
never found in the analysis interval (verified on ngspice 46).

**Aggregation.**

- Rollup (`measurements[]`): a measurement `absent` at some corners and `pass`
  at others rolls up `pass`; `absent` at every corner rolls up `absent`.
  `worst_case` skips absent entries (it is `null` when all are absent).
- Monte Carlo: absent samples are excluded from every statistic and counted in
  a separate `absent` field of the statistics block -- present only for a
  measurement declared `optional` -- so `n + errored + inconclusive + absent`
  is the sample total and `errored` keeps meaning "the simulation failed".
- Coverage: an absent value is not an observation; a run in which *every*
  declared check is absent has nothing checked and reports `not_checked`
  (exit 4), never a vacuous `pass`.
- `klt yield` reading a Monte Carlo report counts a `null` sample as errored,
  so absent samples count there as errored; it does not yet distinguish them.

**Scope.** Supported on `spice` and `expr` measurements of the scalar
`analysis`. `optional: true` on a `crossings` entry (a missing series is a read
failure; zero crossings is already a count of `0`) or inside
`analysis_steps[]` (step failures and unavailable derived inputs must stay
errors) is refused with an application error rather than ignored.

**Backends.** The flag travels in the request document, so `remote` and `batch`
runs honour it. A runner older than the client may not know `optional` and would
grade the corner `error`; the batch backend's runner/client version check
(`request.batch.runner_version_check`, see "Runner/client `klt` version skew")
catches a known skew before launch, but `klt` does not separately detect whether
a given older runner understands `optional`. A `remote` host's `klt` version is
not checked.

## Threshold-crossing time series (`measurements[].crossings`)

Issue [#2868](https://github.com/2AMLogic/klayout-tools/issues/2868). A
cycle-by-cycle reduction (period, jitter, phase drift between oscillators,
lock/slip detection) needs the **sequence** of times a signal crosses a
level. With `spice`/`expr` that takes one `.meas ... WHEN v(x)=<thr>
RISE=<k>` card per edge — 160 cards for 4 signals x 40 edges — and the number
of observable cycles is frozen when the request is written. One `crossings`
declaration returns the whole ordered list instead:

```json
{
  "name": "t_ro1",
  "crossings": {
    "signal": "v(ro1)",
    "level": 0.9,
    "edge": "rise",
    "from": "10n",
    "max": 200
  }
}
```

| Key      | Required | Meaning |
| -------- | -------- | ------- |
| `signal` | yes      | One-line ngspice vector expression (`"v(ro1)"`, `"v(outp) - v(outn)"`, `"i(vdd)"`), evaluated against the transient's saved vectors. |
| `level`  | yes      | Finite number, in the signal's own unit. |
| `edge`   | no       | `"rise"` (default), `"fall"`, or `"either"`. |
| `from`/`to` | no    | Window, inclusive, in seconds: a number or a SPICE time literal (`"10n"`). A crossing outside it is not counted. |
| `max`    | no       | Cap on the returned times, integer in `[1, 100000]`; default `1000`. |

`crossings` is a third measurement form: an entry declares **exactly one** of
`spice`, `expr`, or `crossings` (declaring two is exit 1). It requires
`analysis.kind: "tran"`; an unknown key, a missing `signal`/`level`, an
unknown `edge`, a malformed window or `max` are all refused up front
(`SimError`, exit 1) with a corrective message. Optional `unit`, `limits`,
`plausible_range` and `k_sigma` apply to the entry's `value` (the count).

### Response

The entry is an ordinary `corners[].measurements[]` entry plus one additive
member:

```json
{
  "name": "t_ro1",
  "value": 212,
  "unit": null,
  "status": "pass",
  "margin": null,
  "crossings": {
    "times": [1.0423e-08, 1.9871e-08, "..."],
    "edges": ["rise", "rise", "..."],
    "truncated": true
  }
}
```

- `value` is the **number of crossings found inside the window**, before the
  `max` cap — still a number, so `limits` grade it like any scalar
  (`"limits": {"min": 40}` = "at least 40 edges were observed"), and Monte
  Carlo statistics and coverage need nothing new. Zero crossings (the level is
  never reached) is `value: 0`, `times: []`, not an error. A
  crossings-only request is therefore a checked request: it never degrades to
  `status: "not_checked"` / exit 4.
- `crossings.times` are seconds, ascending, at most `max` long; `edges[i]` is
  the polarity (`"rise"`/`"fall"`) of `times[i]` — informative for
  `edge: "either"`. `truncated` is `true` exactly when `value >
  len(times)`: the cap dropped later crossings; it is never silent.
- `crossings` is present only when the series was extracted. When the signal
  does not resolve (unknown node, or a vector the netlist's own `.save` cards
  dropped under `options.save_mode: "netlist"`), the entry is `status:
  "error"`, `value: null`, no `crossings` member, plus a `code:
  "measurement"` diagnostic naming the signal — like any measurement that
  produced no value. One unresolvable signal does not affect the others.

### How times are computed

Each crossing is **linearly interpolated** between the two samples that
bracket the level (`t = t0 + (level - y0)(t1 - t0)/(y1 - y0)`), so its error
is set by the waveform's curvature over one timestep, not by the timestep
itself — nearest-sample extraction would report the simulator's step as
jitter. A segment is a rising crossing when `y0 < level <= y1` and a falling
one when `y0 > level >= y1`: a sample exactly on the level counts once, and a
signal that merely *starts* above the level has no crossing at t=0.
Reductions over the series (period, cycle-to-cycle, TIE) are not computed
here — see issue #56; they can consume `times` directly.

### What `klt sim` generates, and why it works off-host

After the analysis (and after any `options.waveforms` rawfile write and
`expr` lines, so neither the waveform artifact nor an `expr` is affected),
each `crossings` entry adds

```
let __klt_xing_<k> = <signal>
write <corner_dir>/crossings_<k>.raw __klt_xing_<k>
```

to the generated `.control` block (plus `set filetype=ascii` before the
analysis). `klt sim` reads those small rawfiles inside the corner run,
extracts the series, and removes them with the corner's scratch directory
when `options.keep_artifacts` is false. Because this happens in the corner
code itself, a `remote` or `batch` worker (sharded or not) reports the series
inline in its collected report: no waveform artifact, no artifact retrieval,
no per-edge `.meas` card. Like every request-surface addition, the off-host
runner must carry this implementation (see "Runner/client `klt` version
skew"); an older runner refuses the request rather than ignoring it.

### Not supported

- `engine: "xyce"` (exit 1): the dump is ngspice `.control` commands.
- Inside `analysis_steps[]` (exit 1): no per-step series is implemented.

## Cycle-aligned mean (`measurements[].cycle_mean`)

Issue [#2947](https://github.com/2AMLogic/klayout-tools/issues/2947). A
converter or regulator output carries ripple at the switching frequency, so
its DC level is the mean over a **whole number of periods of a reference
clock**, starting on one of that clock's edges. A fixed literal window biases
the mean by up to ripple / cycles-in-window, and the in-deck routes are
closed: `.meas AVG ... TRIG/TARG` is a syntax error, `FROM=`/`TO=` cannot
reference an earlier `.meas` result, and an `expr` is a single scalar
expression. These remain **unsupported**; `cycle_mean` is the supported way:

```json
{
  "name": "vout_avg",
  "unit": "V",
  "cycle_mean": {
    "signal": "v(out)",
    "align": "v(clk)",
    "level": 0.9,
    "edge": "rise",
    "from": "10u",
    "cycles": 8
  },
  "limits": {"min": 1.78, "max": 1.82}
}
```

| Key      | Required | Meaning |
| -------- | -------- | ------- |
| `signal` | yes      | One-line ngspice vector expression to average (`"v(out)"`, `"i(vdd)"`). |
| `align`  | yes      | One-line vector expression of the reference whose edges bound the window. |
| `level`  | yes      | Finite number, in the reference's own unit: the edge threshold. |
| `edge`   | no       | `"rise"` (default) or `"fall"`. (`"either"` is refused: "one cycle" would be ambiguous.) |
| `from`   | no       | Earliest allowed window start, in seconds (a number or a SPICE literal such as `"10u"`); default 0. The window starts at the first reference edge at or after it. Named `from` to match `crossings`. |
| `cycles` | yes      | Integer `>= 1`: the window spans this many reference periods (`cycles + 1` edges). |

`cycle_mean` is a fourth measurement form, mutually exclusive with `spice`,
`expr` and `crossings` (declaring two is exit 1). It requires `analysis.kind:
"tran"`, is refused for `engine: "xyce"` and inside `analysis_steps[]`, and
rejects unknown keys by name. Optional `unit`, `limits`, `plausible_range`
and `k_sigma` apply to the mean.

### Response

An ordinary `corners[].measurements[]` entry plus one additive member (no
`schema_version` bump):

```json
{"name": "vout_avg", "value": 1.7993, "unit": "V", "status": "pass",
 "margin": 0.0093,
 "cycle_mean": {"t_start": 1.0e-05, "t_end": 1.8e-05, "cycles_used": 8}}
```

`value` is the mean, a plain number, so `limits`, `plausible_range`, Monte
Carlo statistics and coverage need nothing new. `cycle_mean` is present only
when a mean was computed.

### Averaging method

The simulator's timestep is non-uniform, so a plain sample mean is biased
toward wherever it took small steps. `klt sim` instead computes the
**time-weighted (trapezoidal) integral** of the piecewise-linear signal over
`[t_0, t_N]` divided by `t_N - t_0`, where `t_0 ... t_N` are the first
`cycles + 1` reference crossings at or after `from`, each **linearly
interpolated** (same edge finder as `crossings`) and with the signal
interpolated at those exact times.

### Failure modes

None is a silent number: the entry is `status: "error"`, `value: null`, no
`cycle_mean` member, plus a `code: "measurement"` diagnostic (the existing
code; no new `diagnostics[].code` value) whose `message` starts with one of
these stable reason tokens in brackets, e.g. `[cycle_mean_insufficient_edges] ...`:

| Reason token | Cause |
| ------------ | ----- |
| `cycle_mean_vector_missing` | `signal` or `align` did not resolve (unknown node, or dropped by `options.save_mode: "netlist"`), or its rawfile was unreadable. |
| `cycle_mean_no_reference_crossing` | The reference never crosses `level` (with that `edge`) at or after `from`. |
| `cycle_mean_insufficient_edges` | Fewer than `cycles + 1` aligned edges remain after `from`. |

### What `klt sim` generates, and why it works off-host

After the analysis (and after any `crossings` lines), each `cycle_mean`
entry adds

```
let __klt_cmean_<k>_sig = <signal>
let __klt_cmean_<k>_ref = <align>
write <corner_dir>/cycle_mean_<k>.raw __klt_cmean_<k>_sig __klt_cmean_<k>_ref
```

to the generated `.control` block (plus `set filetype=ascii`). The reduction
runs inside the corner run, so a `remote`/`batch` worker reports the scalar
inline with `options.keep_artifacts: false` and no waveform artifact. As with
`crossings`, the off-host runner must carry this implementation (see
"Runner/client `klt` version skew").

## AC return-ratio metrics (`measurements[].transfer`)

The public waveform JSON stores `ac` samples as magnitudes, so phase cannot be
recovered from it. `transfer` grades one stability metric of the complex ratio
`num / den` of an `ac` analysis, computed from the complex samples
([#2923](https://github.com/2AMLogic/klayout-tools/issues/2923), numerics from
[#2922](https://github.com/2AMLogic/klayout-tools/issues/2922)):

```json
{
  "netlist": "loop.spice",
  "analysis": {"kind": "ac", "args": "dec 50 1 1e9"},
  "measurements": [
    {"name": "pm", "transfer": {"num": "v(out)", "den": "v(in)", "metric": "phase_margin_deg"},
     "unit": "deg", "limits": {"min": 45}},
    {"name": "ugf", "transfer": {"num": "v(out)", "den": "v(in)", "metric": "unity_crossing_hz"}}
  ]
}
```

- `transfer` is a fifth measurement form, mutually exclusive with `spice`,
  `expr`, `crossings` and `cycle_mean`. It has exactly `num`, `den`, `metric`.
- `num` / `den` are single-ended `v(node)` selectors: one non-empty node token
  with no whitespace, control character, comma or parenthesis. Matching is
  case-insensitive. Arbitrary expressions and differential selectors are not
  supported.
- `metric` is one of: `low_frequency_magnitude` (linear `|T|` of the first
  valid sample), `unity_crossing_hz` (Hz), `phase_at_unity_deg` (degrees,
  unwrapped), `phase_margin_deg` (`180 + phase_at_unity_deg`, degrees),
  `gain_margin_db` (dB), `unity_crossing_count` (number). `T = num / den` uses
  the **negative-feedback convention**: negate the numerator in the bench if
  yours is the opposite. Interpolation, unwrapping and invalid-sample rules are
  those of `klayout_tools.sim_transfer`.
- Requires `analysis.kind: "ac"`. Malformed bodies, conflicting forms,
  duplicate measurement names and non-AC use fail before the engine launches.
- `value` is graded by the usual `limits`/`plausible_range`. An unavailable
  metric is `status: "error"`, `value: null` (it can never pass a limit) with a
  `code: "measurement"` diagnostic starting `[<reason>]`:

| Token | Meaning |
|---|---|
| `transfer_vector_missing` | `num` or `den` did not resolve (unknown node, or omitted by `options.save_mode: "netlist"`, which is never overridden). |
| `transfer_raw_invalid` | The AC rawfile was unreadable, malformed or had an invalid frequency axis. |
| `transfer_metric_unavailable` | The metric has no value (e.g. no falling 0 dB crossing, zero denominator); the message names the `sim_transfer` reason. |

The entry also carries an additive `transfer` object `{num, den, metric}`.
One rawfile and one ratio computation are used per unique `num`/`den` pair, and
the capture does not depend on `options.waveforms`.

**Local only.** `backend: "remote"` / `"batch"` requests (explicit or via
`KLT_SIM_BACKEND`) that declare `transfer` are refused before provisioning with
an unsupported-backend error; worker support is tracked in
[#2924](https://github.com/2AMLogic/klayout-tools/issues/2924). `engine:
"xyce"` is refused too. After the analysis, each unique pair adds
`let __klt_xfer_<k>_num = <num>`, `let __klt_xfer_<k>_den = <den>` and
`write <corner_dir>/transfer_<k>.raw ...` to the generated `.control` block.

## Source energy over a transient window (`measurements[].energy`)

Issue [#3033](https://github.com/2AMLogic/klayout-tools/issues/3033). Energy
and standby-power studies need what an independent source exchanged over a
window. With `spice`/`expr` that takes three behavioural `B` sources and four
`.meas ... INTEG` cards per source. One `energy` declaration returns it
instead:

```json
{
  "name": "e_vdd",
  "energy": {"source": "vdd", "voltage": "v(a)", "from": "5n", "to": "34n"}
}
```

| Key       | Required | Meaning |
| --------- | -------- | ------- |
| `source`  | yes      | Name of an independent voltage source in the deck (`"vdd"`); its current is read as `i(vdd)`. |
| `voltage` | yes      | One-line ngspice vector for the source's positive-terminal voltage relative to ground (`"v(a)"`). |
| `from`/`to` | no     | Window in seconds, a number or SPICE time literal. Default: the whole transient. Must lie inside the `tran` analysis and `from < to`. |

`energy` is a further measurement form: an entry declares **exactly one** of
`spice`, `expr`, `crossings`, `cycle_mean`, `transfer` or `energy` (two is exit 1). It requires
`analysis.kind: "tran"`. Unknown keys, a missing `source`/`voltage`, a
malformed or inverted window, or a window outside `analysis.args` are refused
up front (`SimError`, exit 1). Optional `unit`, `limits`, `plausible_range`
and `k_sigma` apply to `value` (the net energy).

### Response

An ordinary `corners[].measurements[]` entry plus one additive member (no
`schema_version` bump):

```json
{
  "name": "e_vdd",
  "value": 8.0e-11,
  "unit": null,
  "status": "pass",
  "margin": null,
  "energy": {"net": 8.0e-11, "gross": 8.0e-11, "recovered": 0.0,
             "charge": -8.0e-11, "from": 1e-08, "to": 9e-08}
}
```

- `value` is the **net energy in joules**, so `limits`, `plausible_range` and
  Monte Carlo statistics grade it like any scalar. A source that does not
  resolve gives `status: "error"`, `value: null`, no `energy` member and a
  `code: "measurement"` diagnostic; other measurements are unaffected.
- **Sign convention.** SPICE reports a source's current as positive *into its
  positive terminal*, so the power the source *delivers* is `p = -v * i`.
  `gross` = integral of `max(p, 0)` (delivered), `recovered` = integral of
  `max(-p, 0)` (returned to the source: ideal sources and discharging
  capacitors do this, and `net` alone hides it), `net = gross - recovered`,
  `charge` = integral of `i`. For a DC source `net = -V * charge`.
- **Average power / standby power** is not computed; divide `net` by `to -
  from`.

### Integration method

`p` is sampled at the simulator's timepoints and taken piecewise linear
between them; the window edges are interpolated, and a segment that changes
sign is split at the zero, so `gross` and `recovered` are exact for that
model. The product of two sampled vectors is not itself linear between
samples, so the result differs from the true integral by O(timestep^2); it is
a trapezoidal estimate, not an exact value.

### What `klt sim` generates

After the analysis, each `energy` entry adds

```
let __klt_en_<k>_v = <voltage>
let __klt_en_<k>_i = i(<source>)
write <corner_dir>/energy_<k>.raw __klt_en_<k>_v __klt_en_<k>_i
```

to the generated `.control` block, and `klt sim` reduces those small rawfiles
inside the corner run. So `options.keep_artifacts: false` and `remote`/`batch`
workers work with no waveform artifact, and a request without `energy`
generates a byte-identical deck.

> **Warning: do not scale a hand-written power node.** Hand-building a
> behavioural power node scaled to keep femtowatt hold power above `vntol`
> (e.g. `v = -v(a)*i(vs)*1e12`, power in pW) made ngspice fail at the
> *initial* timepoint (`Timestep too small; initial timepoint: trouble with
> node "<power node>"`), with and without `uic`; the unscaled node (W)
> converged. `energy` adds no behavioural nodes, so it cannot hit this; prefer
> it over hand-written `B` sources.

### Not supported

- `engine: "xyce"` (exit 1): the dump is ngspice `.control` commands.
- Inside `analysis_steps[]` (exit 1): no per-step energy is implemented.
- AC/noise/DC analyses; one window per entry.

## Several solves per corner (`analysis_steps[]`)

The scalar `analysis` runs **one** analysis per corner. Many analog figures of
merit are defined *across* two or more solves of the same corner, with a
source changed between them and the reported number computed over the
results: a load-regulation `out(load_lo) - out(load_hi)`, a line sensitivity
`d(out)/d(in)` between two supply points, a ratio of two DC solves. The only
way to get these used to be a hand-written `.control` loop. That also meant
the grid could not use any `klt sim` backend, `remote` and `batch` included,
because a deck that is not a request cannot be shipped as one
([#2482](https://github.com/2AMLogic/klayout-tools/issues/2482)).

`analysis_steps[]` is an additive alternative to `analysis` (declare exactly
one of the two). Each step has a stable unique name, an analysis, optional
source overrides applied before its solve, and its own measurements. The
top-level `measurements[]` then become **derived** expressions over
step-qualified results, graded through `limits` like any other measurement.
`klt` composes the deck itself. There is no raw `.control` escape hatch.

Worked example: a two-point load-regulation figure:

```json
{
  "netlist": "reg.spice",
  "analysis_steps": [
    {
      "name": "light",
      "analysis": { "kind": "op", "args": "" },
      "alter": { "RL": 1000 },
      "measurements": [{ "name": "vout", "expr": "v(out)", "unit": "V" }]
    },
    {
      "name": "heavy",
      "analysis": { "kind": "op", "args": "" },
      "alter": { "RL": 100 },
      "measurements": [{ "name": "vout", "expr": "v(out)", "unit": "V" }]
    }
  ],
  "measurements": [
    {
      "name": "load_reg",
      "expr": "light.vout - heavy.vout",
      "unit": "V",
      "limits": { "max": 0.3 }
    }
  ]
}
```

A line sensitivity has the same shape: alter the input source (`"alter":
{"VIN": 3.0}` / `{"VIN": 3.3}`) and divide, as in `"expr": "(hi.vout -
lo.vout) / 0.3"`.

A PWL-declared stimulus (`Vx n 0 dc 0 pwl(0 0 1u 0 2u 3.3 1 3.3)`) is
retargeted per step with the PWL form, so independent cold-start points can
differ in the high level of the enable without one request per point:

```json
"analysis_steps": [
  {"name": "v33", "analysis": {"kind": "tran", "args": "10n 3u"},
   "alter": {"Vx": {"pwl": [0, 0, 1e-6, 0, 2e-6, 3.3, 1, 3.3]}}},
  {"name": "v18", "analysis": {"kind": "tran", "args": "10n 3u"},
   "alter": {"Vx": {"pwl": [0, 0, 1e-6, 0, 2e-6, 1.8, 1, 1.8]}}}
]
```

which emits `alter @Vx[pwl] = [ 0 0 1e-06 0 2e-06 3.3 1 3.3 ]` before the
first solve and the 1.8 V vector before the second.

### Request rules

Every rule below is checked **before any corner is dispatched** (application
error, exit 1).

- **Steps.** `analysis_steps` is a non-empty array. A one-step sequence is
  valid. Each step is `{name, analysis: {kind, args}, alter?,
  reset?, measurements?}`.
  - `name` must match `[A-Za-z_][A-Za-z0-9_]*` and be unique
    (case-insensitive).
  - `analysis.kind` must be one of `op`, `dc`, `ac`, `tran`, `noise`, `sp`,
    `tf`, `pz`, or `sens`. A kind may repeat across steps.
  - `analysis.args` is a single-line string.
- **`alter`** maps a top-level source or passive element (`V`/`I`/`R`/`C`/`L`
  name) to a finite number. The overrides are applied as `alter` commands
  before that step's solve, in declaration order.
  - A source the netlist declares with a `pwl(...)` waveform takes a PWL
    vector instead: `"alter": {"VEN": {"pwl": [t0, v0, t1, v1, ...]}}` (an
    even-length flat list of finite numbers, at least two points, times
    non-decreasing; a repeated time is a step edge). It is emitted as
    `alter @VEN[pwl] = [ t0 v0 t1 v1 ... ]` before that step's solve, so
    each step can run its own stimulus. The form is refused for any target
    that is not a `pwl`-declared source (a DC, PULSE or SIN source, or a
    passive).
  - Overrides are **cumulative**, as in ngspice: a later step sees an
    earlier step's value unless it alters the element again. Without
    `reset` (below), steps share circuit **and** solver state (each solve
    starts from the previous one's solution).
  - Refused:
    - a hierarchical or device-parameter target;
    - a target repeated within one step;
    - a target that is also a `corners.supply_v` key, because the step would
      silently replace the corner's own value;
    - a scalar value for a source declared with an explicit transient
      waveform, where `alter` has no effect (use the `{"pwl": [...]}` form
      for a PWL source; the same rule as the supply axis,
      [#2706](https://github.com/2AMLogic/klayout-tools/issues/2706));
    - a name the netlist declares only as a top-level `.param` (no element
      of that name), where `alter` also has no effect -- the same rule and
      the same lexical limits as the supply axis
      ([#2867](https://github.com/2AMLogic/klayout-tools/issues/2867)).
- **`reset`** (optional boolean, default `false`;
  [#3103](https://github.com/2AMLogic/klayout-tools/issues/3103)): `true`
  starts the step from a freshly parsed circuit, so independent
  configurations of one analysis can share a request (one job, not one per
  configuration). A non-boolean value is refused by name.
  - Isolation contract: a reset step is fully determined by the corner's
    `supply_v` plus its own `alter`. Earlier steps' `alter`s and solver state
    do not carry in.
  - ngspice `reset` reverts every `alter` and drops the saved set, so the deck
    re-emits, in order: `reset`, `save all` (under the same condition as the
    corner prefix), the corner's `supply_v` `alter`s, then the step's own
    `alter`s.
  - Earlier steps' plots stay readable, so `<step>.<name>` references and
    derived measurements across a reset step keep working.
  - `reset: true` on the first step is accepted and has no effect on results.
  - Decks without any `reset` are unchanged. Like any new request field it
    needs a runner with a matching `klt` version on the `remote`/`batch`
    backends.
- **Step measurements** use the same two forms as the top level, and each
  is evaluated against **that step's own solve**:
  - `expr`: any ngspice expression reducing to one real scalar. It may also
    read an **earlier** step's result as `<step>.<name>`.
  - `spice`: a `.meas` card whose type matches the step's `kind` and whose
    card name equals the measurement `name`. It runs as a `meas` control
    command right after that step's analysis.

  Names follow the `expr` naming rule and are unique within the step. In the
  response, a step measurement is reported as `<step>.<name>` (e.g.
  `light.vout`).
- **Derived measurements**: the top-level `measurements[]`.
  - Each must be an `expr`. A file-scope `.meas` card would run after every
    analysis, so it is refused; put it in the matching step instead.
  - Each must reference at least one `<step>.<name>` result.
  - It may also name an **earlier** derived measurement by its bare name
    (a dependency chain).
  - A bare step-measurement name without its `<step>.` qualifier is refused
    with a hint.
- **References** must point backwards. An unknown step, an unknown
  measurement, a forward reference, and a self-reference are each refused,
  as is a step measurement that reads its own or a later step.
- **Ambiguous names.** Step results print to the log as `<step>__<name>`;
  derived results print under their own names. Two results that would print
  under the same key (case-insensitive) are refused.
- **Unsupported options are refused by name.** `options.waveforms` and
  `--plot` are refused because the waveform artifact captures one analysis.
  `engine: "xyce"` is refused (see "Xyce engine" above). Nothing in a step
  sequence is silently ignored.
  - `options.fail_fast_probe` and the timeout preflight still apply only to
    a scalar `tran` analysis. For a step sequence they do nothing, as they
    already do for any non-`tran` scalar analysis.

### What `klt sim` generates

Each corner gets one deck with one `.control` block, and the steps run in
declaration order. The block starts with the usual `save all` and the
corner's `alter` cards. The deck generated for the example above:

```
.control
save all
alter RL=1000
setplot new
echo klt_step_begin 0 $curplot
op
echo klt_step_end 0 $curplot
set klt_plot_0 = $curplot
let vout = v(out)
let light__vout = vout
print light__vout
alter RL=100
setplot new
echo klt_step_begin 1 $curplot
op
echo klt_step_end 1 $curplot
set klt_plot_1 = $curplot
let vout = v(out)
let heavy__vout = vout
print heavy__vout
setplot new
let load_reg = {$klt_plot_0}.vout - {$klt_plot_1}.vout
print load_reg
quit
.endc
```

Each part guards against a plausible but wrongly paired number:

- **`setplot new` before each solve.** A failed analysis then leaves an empty
  plot current, so the step's measurements find no vectors instead of
  silently reading the previous step's solve.
- **The `echo` markers** record whether the analysis produced its own plot.
  Step failure is read from these markers, not guessed from measurements.
- **Plot-qualified references.** A step-qualified reference
  (`light.vout`) becomes ngspice's own plot-qualified vector
  (`{$klt_plot_0}.vout`), so it always reads that step's solve at full
  double precision, not a printed and rounded copy.
- **Unique log keys.** Each step result is copied to `<step>__<name>`, so two
  steps that both measure `vout` are harvested separately.
- **Derived expressions run last**, in a fresh empty plot. A bare node
  reference such as `v(out)` therefore cannot resolve against whichever step
  ran last.

### Response

The report keeps the existing measurement vocabulary (`value`, `unit`,
`status`, `margin`, `limits`, `worst_case`, Monte Carlo statistics). Three
fields are added, and they appear only for an `analysis_steps[]` request:

- `step` on each step-scoped measurement entry, naming its source step;
- `derived_from` on any entry computed from other results, listing their
  `name`s;
- `corners[].steps[]`: one `{name, kind, status: "ok"|"error"}` per step.

From the example run (ngspice 46):

```json
"measurements": [
  { "name": "light.vout", "value": 3.267327, "unit": "V", "status": "pass", "margin": null, "step": "light" },
  { "name": "heavy.vout", "value": 3.0, "unit": "V", "status": "pass", "margin": null, "step": "heavy" },
  { "name": "load_reg", "value": 0.2673267, "unit": "V", "status": "pass", "margin": 0.0326733,
    "derived_from": ["light.vout", "heavy.vout"] }
],
"steps": [
  { "name": "light", "kind": "op", "status": "ok" },
  { "name": "heavy", "kind": "op", "status": "ok" }
]
```

**Failure propagation is decided by `klt`, never inferred from ngspice
output:**

- **A failed step** gets `steps[].status: "error"` and one `step_failed`
  diagnostic. Its own measurements are reported `value: null`, `status:
  "error"`.
- **A result whose input has no value** (a failed step, or an earlier
  measurement that produced none) is **not graded**. It reports
  `status: "error"` with a `derived_input_unavailable` diagnostic naming the
  missing inputs. A dependent figure is never graded as a pass or fail from a
  partial set of inputs.
- **A derived expression that itself failed** after all its inputs were
  available (for example, a division by zero) reports an ordinary
  `measurement` diagnostic. This keeps "an input step failed" distinct from
  "the expression failed".
- **Independent evidence stays inspectable.** Every other step's results in
  the same corner keep their values. Steps after a failed one still run.

As with any `error` measurement, the corner's `status` becomes `"error"`.
Both new codes can be selected in `options.fail_on_diagnostic`.

### Backends, resume, and Monte Carlo

- **`remote` and `batch`** accept an `analysis_steps[]` request unchanged.
  The sequence is request text, and the off-host worker's own `klt sim`
  generates the same deck from the forwarded request document. The caller
  does no post-processing, and `local-parallel` also needs nothing new.
- **Checkpoint/resume.** The checkpoint fingerprint includes the complete
  ordered step definition: names, order, analyses, overrides, and every
  step-scoped and derived measurement. Changing any intermediate solve
  invalidates the checkpoint, so stale evidence is never reused.
- **Corner axes and `monte_carlo`** expand exactly as before. The whole
  sequence runs once per corner or per sample, in one ngspice process with
  one solver session. Cross-corner and cross-sample aggregation remain out
  of scope.
- **`--op-lint`** also scans each step's measurements for `v(<node>)`
  references, reported as `<step>.<name>`.

### Migrating a hand-rolled `.control` bench

Write one step for each `alter` + solve pair. Turn each `let vX = v(out)`
into a step `expr` measurement. Write the cross-point arithmetic as a derived
measurement over `<step>.<name>` references, with `limits` attached. Then
remove the `.control` block from the netlist body (see "Netlist convention"
above). The bench can then run on any backend.

## Off-host backends stage the netlist's `.include` closure

A netlist may `.include`/`.inc` other files — that is `klt pex`'s whole
testbench contract (a thin testbench that `.include`s its DUT, see
[`pex.md`](pex.md)), and the repo's own worked examples are written that
way. The off-host backends (`remote`, `batch`) therefore stage **the
netlist's resolved include closure**, not just the netlist file
([#2485](https://github.com/2AMLogic/klayout-tools/issues/2485)):

- Every `.include`/`.inc` target that resolves to a readable file on the
  submitting host is uploaded into the job directory (`inputs/` for
  `batch`) alongside `netlist.cir`, **recursively** — an included file's own
  includes are staged too, resolved against *its* directory, which is
  ngspice's own rule.
- The directive naming each staged file is rewritten to the flat,
  job-relative name it was staged under, so nothing off-host ever depends on
  a path that only exists on the submitting host. Staged names are generated
  from the basename (sanitized, de-duplicated on collision), never taken
  from the netlist's own text.
- **An include that resolves nowhere on this host fails the submit** with a
  named error (exit `1`) naming the file, line, target, and where it was
  looked for — before any instance is provisioned or any object is written
  to S3. Previously such a deck was shipped anyway and came back as a full
  set of `error`/`unavailable_measurement` corners indistinguishable from a
  real regression.
- Two classes of include are deliberately **left verbatim** when staging,
  because the *executing* host owns their resolution: a target naming an
  environment variable (`$PDK_ROOT/...`), and a target resolving under the PDK root the
  request already forwards (`models.pdk_root`, else this host's `$PDK_ROOT`)
  — the model library is baked into the fleet image and is never pushed per
  job (see "Remote backend" above).
- **`$VAR`/`~` in `.include`/`.inc` targets are expanded on the executing
  host** (every backend, including `local`), when the per-corner deck is
  composed -- ngspice itself never expands environment variables in include
  paths. The netlist body's include lines are rewritten into a copy beside
  the corner deck (`${VAR}`, quoted targets and `~` are handled; includes
  inside included files are not followed). A variable that is unset on the
  executing host yields a `missing_include` diagnostic naming the netlist,
  line and variable; an include ngspice itself fails to load (`Could not
  find include file ...`, or ngspice 42's `.include filename missing`) also
  carries `missing_include`.
- `.lib` cards in the netlist are not followed: `.lib` is the model-library
  channel, which `models`/`models.pdk` resolve on the executing host by
  default (see "Model library resolution" below) — or, with
  `options.stage_model_inputs: true`, ship with the job (next section).

  Because `klt sim` leaves a netlist-body `.lib` card **verbatim** (only
  `.include`/`.inc` lines are rewritten or `$VAR`-expanded there), the card
  reaches ngspice exactly as written. ngspice 46 does not expand an
  unquoted `$PDK_ROOT` inside a `.lib` card: `.lib $PDK_ROOT/<file> <section>`
  silently never loads the section and the failure surfaces much later as
  `unknown subckt`, whereas `.lib "$PDK_ROOT/<file>" <section>` (quoted)
  works. `.include $PDK_ROOT/...` is unaffected because klt expands it
  itself. Quote the path in `.lib` cards of a netlist that leaves
  `$PDK_ROOT` for the executing host to resolve.

A closure larger than 64 files or 32 MiB is refused with the same named
error rather than silently pushed — a closure that big is a host-resident
library tree, which belongs under the PDK root or behind an environment
variable.

`local`/`local-parallel` are unaffected: they run on the host that resolved
the paths in the first place.

## Staging the request's model inputs (`options.stage_model_inputs`)

By default the off-host backends treat the runner image as the PDK
contract: `models.lib`, per-section corner libraries and `.osdi` binaries
are resolved **on the executing host**, so the PDKs `remote`/`batch` can run
are exactly the ones baked into the image. A request whose models are not
in the image — a PDK outside the baked set, a locally-modified library, or
OSDI binaries built on top of the distributed sources — opts in to shipping
them with the job
([#2668](https://github.com/2AMLogic/klayout-tools/issues/2668)):

```json
{
  "netlist": "tb.spice",
  "backend": "batch",
  "models": {"pdk": "sg13g2", "lib": "libs.tech/ngspice/models/cornerMOSlv.lib"},
  "corners": {"process": ["mos_tt", "mos_ss"]},
  "options": {
    "stage_model_inputs": true,
    "osdi_preload": ["build/osdi/psp103.osdi", "build/osdi/r3_cmc.osdi"]
  }
}
```

- **Opt-in, boolean.** Absent or `false` is exactly the behaviour above
  (nothing model-related is uploaded). Any non-boolean value is an
  application error (exit `1`) on every backend. On `local`/`local-parallel`
  the option is accepted and has no effect — those backends already read the
  files where they are.
- **What is staged.** `models.lib`, every per-section `lib` of a
  `corners.process` bundle (and the same refs inside a fleet shard's
  internal `_explicit_points`), and every `options.osdi_preload` entry.
  Sources are resolved **on the submitting host** with the same rules a
  local run uses — a relative `lib` joins the PDK variant directory when
  `models.pdk`/`models.pdk_root` is set, otherwise the request file's
  directory; `osdi_preload` entries join the request file's directory — and
  are then uploaded beside `netlist.cir` (`inputs/` for `batch`) under flat,
  sanitized, collision-safe names (`inc1_<name>` on a basename clash; the
  generated `netlist.cir`/`request.json` names are never reused).
- **Self-contained model closure.** Each staged library's own dependencies
  are followed recursively: `.include`/`.inc` targets and **file-bearing**
  `.lib <file> <section>` references are staged and rewritten to their
  staged names. `.lib <section>` … `.endl` section *definitions* are left
  intact. Unlike the netlist's own closure, nothing is left for the
  executing host: `$VAR` targets are expanded here, and targets under the
  PDK root are staged like any other file. A dependency that does not
  resolve to a readable file, a `$VAR` that is unset here, or an
  unsupported `.lib` form (no arguments, more than two, unbalanced quotes)
  is a named error. Shared dependencies are uploaded once; cycles
  (including a library that `.lib`-references itself) terminate.
- **Binaries are opaque.** `.osdi` files are uploaded byte-for-byte, never
  parsed or re-encoded, and `options.osdi_preload` keeps its declared order
  (repeated entries stay repeated). A text file that needs a rewrite must be
  UTF-8; unmodified files ship byte-for-byte from their own path.
- **The caller's request is not modified.** The worker receives a rewritten
  *copy*: model references become job-relative staged names, and an
  internal `_staged_model_inputs: true` marker tells the worker's `klt sim`
  to resolve them against the job directory instead of joining them onto its
  image's PDK directory. `models.pdk` stays in that request — it still
  selects the image.
- **Limits.** The complete staged closure (netlist includes plus every model
  file and binary; the netlist itself is not counted) is capped at **512
  files / 256 MiB**; the netlist's own include closure keeps its 64-file /
  32 MiB cap above. Exceeding either is a named error.
- **Fails before anything is spent.** Every missing file, unresolvable
  dependency and exceeded cap is reported — naming the request field it was
  reached from (`models.lib`, `corners.process[1].sections[0].lib`,
  `options.osdi_preload[2]`, …) plus the file and line of a failing
  dependency — before any S3 write, SSH push, instance launch or fleet
  launch, including for sharded (`remote.hosts > 1`) runs.
- **Image selection is unchanged.** `remote` still requires `models.pdk` to
  pick its AMI, and both backends still refuse a `models.pdk` the fleet has
  no image for (see "Both off-host backends validate `models.pdk` up
  front"). A `batch` request with no `models.pdk` at all runs on the fleet's
  default image with no `pdk_variant` in `job.json`. `environment.remote`
  still records the image that ran (`ami_id`).
- **Runner compatibility is still yours to guarantee.** Staging moves files,
  not toolchains: an `.osdi` must be built for the runner's platform (the
  fleet images are Linux x86-64) **and** the runner's ngspice OSDI ABI, and
  the model files must parse under the runner's ngspice. `klt` does not
  compile binaries or publish images; opting in is the caller's statement
  that the binaries are compatible.
- **Provenance.** The submitter's report carries an additive
  `environment.staged_model_inputs` array — one
  `{name, field, kind, sha256, source_sha256, rewritten}` per staged model file, where
  `kind` is `"library"`, `"osdi"` or `"dependency"`, and `sha256` hashes the
  bytes the worker actually received (a library whose directives were
  rewritten hashes differently from its source; `rewritten` says so; an
  `.osdi` never does). `source_sha256` hashes the *original* source bytes
  (equal to `sha256` when `rewritten` is false), so a rewritten library can
  still be tied back to its source. On the submitter, `models_lib_sha256` is
  always the *client's* resolution; `environment.models_lib_sha256_scope` says
  so (`"client"` for off-host backends, `"local"` otherwise). For the `batch`
  backend the runner's own view is `environment.remote.runner_model_inputs`
  (`[{name, sha256}]`, hashes of the staged model files the job instance
  received, bounded to 64 entries); it is `null` — not `[]` — when the runner
  did not report it (older runner, no staging, absent capture). The `remote`
  backend has no worker capture and does not emit it. On the worker, the existing `models_lib_sha256`,
  `osdi_preload[].sha256` and `corner_section_libs[].sha256` hash those same
  staged files. The array is present only when the option staged inputs for
  an off-host backend.

## OSDI (Verilog-A) model preload

ngspice can only instantiate a Verilog-A compact model through an OSDI
shared library, loaded with the **control** command `pre_osdi <file>`
(`pre_`-prefixed commands run before the circuit is parsed). Several open
PDKs ship their core devices that way — IHP's `sg13g2` MOSFETs are PSP103
Verilog-A, compiled to `libs.tech/ngspice/osdi/*.osdi` by
`scripts/fetch-sg13g2-sim-toolchain.sh` (see
[`pdks/README.md`](../../pdks/README.md)). Without the preload every device
fails with `Unable to find definition of model …` and every corner errors.

`options.osdi_preload` is the first-class hook for this
([#2513](https://github.com/2AMLogic/klayout-tools/issues/2513)):

```json
"options": {
  "osdi_preload": [
    "$PDK_ROOT/ihp-sg13g2/libs.tech/ngspice/osdi/psp103.osdi"
  ]
}
```

- **Deck shape.** Each entry becomes one `pre_osdi <absolute path>` line,
  in declared order, at the top of the `.control` block `klt sim` already
  generates — ahead of every `alter` and the analysis command. The netlist
  body stays a plain circuit body (see "Netlist convention" above); neither
  a second `.control` block in the body nor a host-level `~/.spiceinit` is
  needed. The same lines reach the `fail_fast_probe` calibration deck and
  the `--op-lint` operating-point deck, so every deck a request produces
  sees the same devices.
- **Resolution.** Same rule as `models.lib`'s literal-path shape: `$VAR`
  and `~` expand, a relative path joins the request file's own directory.
- **Validated up front.** Every entry is existence-checked before any
  corner is dispatched — a missing file is an application error (exit `1`,
  `osdi_preload file not found: <path>`), the same posture as `models.lib`'s
  `model library not found`, never a per-corner ngspice failure deep inside
  the run. A non-array value or an empty entry is refused the same way.
- **Provenance.** `environment.osdi_preload` lists each preloaded library, in
  load order, as `{name, path, scope, sha256}` — `path`/`scope` normalised
  exactly like `environment.models_lib` (an out-of-repo PDK file reports
  `{"path": null, "scope": "external"}`), `sha256` pinning the binary's
  content. An `.osdi` is as much a part of "which models produced this
  result" as the `.lib` is. The field is present only when the request
  declares the option. A resumable sweep's checkpoint fingerprint also keys
  on these hashes, so a rebuilt `.osdi` invalidates the checkpoint like an
  edited model library does.
- **Off-host backends refuse it by default.** `remote`/`batch` fail the
  request with a named error (exit `1`) rather than staging the files —
  unless the request sets `options.stage_model_inputs: true`, which uploads
  the binaries byte-for-byte in preload order (see "Staging the request's
  model inputs" above; compatibility with the runner's platform and ngspice
  is then the caller's responsibility). Unlike the
  `.include` closure (text files, staged per #2485 above), an `.osdi` is a
  host-architecture shared library compiled against one ngspice build's OSDI
  interface: a macOS arm64 `.osdi` staged onto the fleet's Linux x86-64
  image would fail to load, and a path that happens to exist on both hosts
  would silently pin a different binary than the one hashed here. When the
  off-host backend came from the host default (`$KLT_SIM_BACKEND`), the run
  steps back to `local` instead, like every other "this run cannot leave
  this host" case. Run OSDI-model sweeps on `local`/`local-parallel`.
- **Xyce refuses it.** `pre_osdi` is an ngspice control command, and Xyce
  loads Verilog-A as its own compiled plugins (`-plugin`), not OSDI
  libraries — `engine: "xyce"` with `options.osdi_preload` is an application
  error, never a silently dropped preload.

`klt characterize` forwards its own `options.osdi_preload` to this option
(see [`characterize.md`](characterize.md)).

## ASAP7 FinFET (BSIM-CMG) simulation

Netlists from `klt extract --deck asap7` (FinFET, geometry-counted `NFIN`)
simulate in ngspice with the ASAP7 BSIM-CMG models once ngspice has a
BSIM-CMG OSDI module
([#2814](https://github.com/2AMLogic/klayout-tools/issues/2814)). Stock
ngspice has no BSIM-CMG: the pinned PDK's `level = 72` cards fail with
`Only MOS device levels ... are supported in this binary`.

**Provisioning.** `scripts/fetch-asap7-sim-toolchain.sh` (see
[`pdks/README.md`](../../pdks/README.md)) fetches the CMC BSIM-CMG 111.0.0
Verilog-A source (dwarning/VA-Models commit `6099b4e1...`, tarball sha256
`357d6956...`), compiles it with the same pinned OpenVAF-Reloaded
(`osdi_0.4-153-g2e066436`) as the SG13G2 flow, and load-verifies the module
in the `ngspice` on `PATH` (`pre_osdi` + `devhelp` must list `bsimcmg_va`).
It needs ngspice >= 44 built with OSDI and, for the pinned compiler, Linux
x86-64 (elsewhere set `OPENVAF_R_BIN`). The `.osdi` lands in the ignored
`pdks/asap7-sim/osdi/`; nothing fetched or built is tracked.

**Card adaptation** (`klayout_tools.asap7_bsimcmg`, pure text-to-text):

- `adapt_model_cards` rewrites `.model nmos_rvt nmos level = 72 ...` as
  `.model nmos_rvt bsimcmg_va type=1 ...` (`type=-1` for pmos) and drops
  `level` and `version` (the Verilog-A module has neither; the cards say
  `version = 107`, the module is 111.0.0). All other parameters are carried
  over verbatim. The module does not know `coremod`, `capmod` and `nseg`;
  ngspice reports them as `unrecognized parameter ... - ignored`, so those
  three have no effect. `corner_library` wraps the TT/FF/SS cards as
  `.lib tt|ff|ss` sections, so `models.lib` plus `corners.process` makes a
  multi-corner run an ordinary `klt sim` sweep.
- `adapt_extracted_netlist` turns `M<name> d g s b <model> L=.. W=.. NFIN=..`
  into the OSDI element `N<name> d g s b <model> L=.. NFIN=..`. BSIM-CMG has
  no `W` (width is quantised by the fin count), so it is dropped; `NFIN` is
  the **total** fin count and is passed through unchanged (an `NF=`/`FINGERS=`/
  `AS`/`AD`/`PS`/`PD` on the card is refused, since BSIM-CMG's `NF` would
  multiply `NFIN`). KLayout's `\$11` / `VDD$1` names become `11` / `VDD_1`.
  The extractor's `M` card (`finfet_card`) is unchanged: it stays the
  CDL-shaped card LVS compares. Measured: `NFIN=6` draws exactly twice the
  drain current of `NFIN=3`.
- The extracted cell's separate `VDD`/`VDD$1` (tap vs. source) and
  `vsubs`/`VSS` pins are distinct nets; the testbench ties them.

**Request.**

```json
"models": {"lib": "asap7_bsimcmg.lib"},
"corners": {"process": ["tt", "ff", "ss"], "supply_v": {"vdd": [0.7]}, "temperature_c": [25]},
"options": {"osdi_preload": ["pdks/asap7-sim/osdi/bsimcmg.osdi"]}
```

**Prerequisite failures are errors, never passes.** Without the module (or
with an ngspice that cannot load it) every corner is `status: "error"` with
a `prerequisite missing: ...` diagnostic naming the capability:
`unsupported_model_level` (the `level = 72` line above),
`osdi_abi_mismatch` (`NGSPICE only supports OSDI v0.3 but ...`, ngspice <
44) or `osdi_load_failed` (`Library x.osdi couldn't be loaded`). A missing
`.osdi` file is refused up front (`osdi_preload file not found`). The
`unsupported_model_level` diagnostic suppresses the follow-on
`model_bin_range` code, which would misattribute the failure to a width bin.
`tests/test_asap7_bsimcmg_sim.py` skips with the missing capability named
when the corpus, the module or a loading ngspice is absent.

**Verification status (2026-10-13).** Verified end to end on a Linux x86-64
host with ngspice-46 (KLU, OSDI ABI 0.4) after `scripts/fetch-pdks.sh` and
`scripts/fetch-asap7-sim-toolchain.sh`; Xyce was not used. Provenance:

| Item | Value |
|------|-------|
| Simulator | ngspice-46, `pre_osdi` + `devhelp` load of `bsimcmg_va` verified by the fetch script |
| Compiler | OpenVAF-Reloaded `osdi_0.4-153-g2e066436` (sha256 `9c570bf8...`) |
| Model source | BSIM-CMG 111.0.0, dwarning/VA-Models `6099b4e1`, tarball sha256 `357d6956...` |
| Compiled module | `pdks/asap7-sim/osdi/bsimcmg.osdi`, sha256 `4e755d14e901a84600124be5cb73e36302117b6cfc694e52ffc17640945cd9e3` (also in `environment.osdi_preload`) |
| Model cards | lambdapdk 0.2.17 `7nm_{TT,FF,SS}_160803.pm`, TT sha256 `684708eec0f7d5960be1df6a045de811c8dd2d6b0e1ed7d0f805a3f697f262fc` |

Command: `pytest tests/test_asap7_bsimcmg_sim.py` (the extracted
`INVx1_ASAP7_75t_R` / `NAND2xp5_ASAP7_75t_R` from `asap7sc7p5t_28_R.gds.gz`
via `klt extract --deck asap7`, adapted, then `klt sim` transient with
`options.osdi_preload`; Vdd = 0.7 V, 25 C, 1 fF load, input rising edge at
~110 ps). Both extracted `M` cards (`L`, total `NFIN`, no `AS/AD/PS/PD`/`NF`)
were accepted by the BSIM-CMG instance parameter set without changing
`finfet_card`. Observed results (`tpd_fall` is input 50% to output 50% falling):

| Cell | Corner | `vout_low` (V) | `vout_high` (V) | `tpd_fall` (ps) |
|------|--------|----------------|-----------------|-----------------|
| INVx1 | TT | 1.06e-7 | 0.7000 | 9.18 |
| INVx1 | FF | 1.18e-7 | 0.7000 | 8.06 |
| INVx1 | SS | 9.30e-8 | 0.7000 | 10.45 |
| NAND2xp5 | TT | 2.84e-7 | 0.69996 | 11.82 |
| NAND2xp5 | FF | 3.17e-7 | 0.69996 | 10.18 |
| NAND2xp5 | SS | 2.51e-7 | 0.69995 | 13.93 |

Rail-to-rail swing and the FF < TT < SS delay ordering hold for both
topologies. This run executed locally through `klt sim` (the report's
`environment.remote` was null); it is a six-simulation functional check, not
a corner grid. A stock ngspice without the module still produces the
`unsupported_model_level` diagnostic above.

## Corner axes

- **`corners.process`** (`array<string | {"name": string, "sections":
  (string | {"lib": string, "section": string})[]}>`, optional) —
  process-corner axis. Each entry is either:
  - a **bare string** — opaque `.lib` section name passed through to
    `models.lib` (e.g. sky130's `tt`/`ss`/`ff`/`sf`/`fs`, or a mismatch
    variant like `tt_mm` — no schema change needed for those). Emits a
    single `.lib <models.lib> <name>` card.
  - a **bundle object** `{"name": str, "sections": [...]}` — a named
    corner backed by *multiple* `.lib` sections, emitted as one `.lib`
    card per entry in `sections`, **in declaration
    order** (ordering matters when the section set has interdependent
    global switch params — see gf180mcu below). `name` is what shows up as
    `corner_id`/the response's `process` field and what `corners.exclude[].process`
    matches against; it need not equal any of the section strings.

    Each `sections[]` entry is itself either a **bare string** (that section
    is read from `models.lib`, emitting `.lib <models.lib> <section>`) or an
    object **`{"lib": str, "section": str}`** naming its *own* model library
    for that section, emitting `.lib <lib> <section>` — see "Per-section
    corner libraries" below.

  Selecting a process corner needs a model library; `models.lib` is only
  required when this axis is present — and, since
  [#2522](https://github.com/2AMLogic/klayout-tools/issues/2522), only when
  at least one selected section actually reads from it (a bundle whose
  *every* section names its own `lib` needs no `models.lib` at all). Omit
  `corners.process` entirely for a request that doesn't care about process
  (single point, no `.lib` card emitted).

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
    Poly resistors (`sky130_fd_pr__res_*`) are covered too: their subcircuits
    draw per-instance `AGAUSS` terms (`res_match`, `rend`) scaled by
    `MC_MM_SWITCH`, so two identical instances differ under `tt_mm` (measured
    on the volare sky130A build `c6d73a35…`; note the term is upper-case, so a
    case-sensitive `grep agauss` misses it). The corner-level `var_mult` is a
    separate term shared by all instances.
  - **ihp-sg13g2**: Section-based like sky130. `cornerMOSlv.lib` /
    `cornerMOShv.lib` define plain `mos_<corner>` sections plus
    `mos_<corner>_mismatch` sections; only the `_mismatch` ones include the
    per-instance `agauss()` mismatch subcircuits (`sg13_lv_nmos`/`sg13_lv_pmos`,
    `sg13_hv_nmos`/`sg13_hv_pmos`). `mos_tt_stat` is process-statistical, not
    mismatch. `family_mismatch` reports `mosfet` `active: true` only when every
    selected `corners.process` section ends in `_mismatch`, `false` when none
    does, and `null` when the section is unknown or mixed.
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

- **`corners.supply_v`** (`object`, optional) — keyed by voltage-source name
  (`vdd`, `vdda`, …), each an array of volts. **Multiple keys sweep together
  by index** (rails move as a set, not a cross product) — all arrays must be
  the same length. Each value becomes an `alter <key>=<value>` command in the
  generated `.control` block, so `<key>` must name a top-level independent
  voltage source (for `.param vdd_val=1.8` / `Vdd vdd 0 DC {vdd_val}`, the key
  is `vdd`, the source -- not `vdd_val`).
  **Not a `.param`.** ngspice evaluates `.param` expressions when it parses
  the deck, and `alter <param>=<value>` addresses no device: ngspice only logs
  `no such device or model name <param>` and simulates the nominal value, so
  a sweep keyed by a parameter used to report N identical `pass` corners
  (#2867). `klt sim` now rejects, with a `SimError` before any probe, local
  run or `remote`/`batch` submission, a key the netlist declares as a
  top-level `.param` (multi-name `.param a=1 b=2` lines and `+` continuations
  included, case-insensitive) with no element of that name; the message names
  the key and, when a top-level voltage source reads the parameter, that
  source. To sweep the parameter itself (a `SIN()` amplitude or frequency, a
  bias current) use a parameter axis, `corners.params` (#3035), rather than
  `supply_v`. A name declared as both a `.param` and an element is the
  element, and stays valid. This check follows `.include`/`.lib` references
  looking for an element of that name; if any reference cannot be resolved
  and read, the key is not rejected (an unresolved declaration is never
  assumed absent). Hierarchical names are never treated as parameters.
  **Unknown keys (#2894).** A key that names no element and no `.param` in
  the fully resolved netlist closure (a typo; matching is case-insensitive)
  is rejected with a `SimError` naming the key, because `alter` on it would
  address nothing. Not rejected when any `.include`/`.lib` reference cannot
  be resolved, nor for hierarchical keys such as `x1.vdd`.
  **Scalar sources only.** ngspice's `alter` silently has no effect on an
  independent voltage source with an explicit transient waveform
  (`PWL`/`PULSE`/`SIN`/`EXP`/`SFFM`/`AM`), so `klt sim` rejects such a request
  up front with a `SimError` naming the target, the waveform and the
  unsupported override -- before any probe, local run or off-host
  (`remote`/`batch`) submission. A mixed grid fails as a whole; a waveform
  source with no `supply_v` entry is unaffected, and bare-value/`DC` sources
  keep the scalar override. The check covers the actual expanded corner points
  (including a fleet shard's `_explicit_points`). It is a lexical inspection of
  the netlist body only (case-insensitive names, `+` continuations, comments
  ignored, `.subckt`-local declarations ignored), **not** a complete SPICE
  parser: `.include`d/`.lib` files and hierarchical targets are not resolved,
  so a waveform declared there is not detected, and an unresolved declaration
  is never assumed to be DC. Rewriting waveform sources is out of scope.
- **`corners.temperature_c`** (`array<number>`, optional) — degrees Celsius,
  one `.temp <value>` card per point. Defaults to `[27]` when omitted.
- **`corners.params`** (`object`, optional, [#3035](https://github.com/2AMLogic/klayout-tools/issues/3035))
  — a numeric design-parameter axis: SPICE parameter names mapped to
  equal-length, nonempty arrays of finite JSON numbers. Use it for a
  parameter-dependent resistor, bias value, trim code, or a waveform
  amplitude/frequency that must vary across PVT *within one request*, without
  pretending the coordinate is a supply voltage.

  ```json
  {"corners": {"temperature_c": [27, 85],
               "params": {"rload": [1000, 2000], "code": [0, 1]}}}
  ```

  This expands to four points, in this order: 27 °C/row 0, 27 °C/row 1,
  85 °C/row 0, 85 °C/row 1 (row *i* is `rload=array[i], code=array[i]`).

  - **Rules.** Names match `[A-Za-z_][A-Za-z0-9_]*`, are normalized to
    lowercase, and may not repeat case-insensitively or start with `mc_`
    (reserved for Monte Carlo). Values are numbers only — no booleans,
    `null`, `NaN`/`Infinity`, or expression strings. `null`, `[]`, unequal
    lengths and duplicate (normalized) rows are rejected. A name may not
    appear in both `corners.params` and `corners.supply_v`
    (case-insensitive); the request is refused before any dispatch.
  - **Expansion order**, outer to inner: process → supply → temperature →
    parameter row. Monte Carlo samples stay inside the expanded point.
    Parameter arrays always zip by index; there are no process-zipped,
    dependent or sparse parameter axes.
  - **What a parameter addresses.** An existing *top-level* `.param`
    declaration, in the request's netlist or anything it `.include`s. The
    requested value replaces the declared default and every expression that
    reads it (instance values, `SIN()` amplitude/frequency, dependent
    `.param`s) is re-evaluated. The deck applies it as `alterparam <name>=<value>`
    for each name, then **one** `reset`, before the saved-set `save all`, the
    supply `alter` commands and the analysis; `.temp`, `.options`, the
    Monte Carlo seed card and `options.osdi_preload` are unaffected. A name no
    top-level `.param` declares is a `SimError` before dispatch (when the
    `.include`/`.lib` closure cannot be fully read, the scan defers and the
    engine's own `alterparam ... not found` instead grades that corner
    `error` with a `netlist` diagnostic). Subcircuit-local parameters and
    arbitrary text injection are not supported, and source cards are never
    rewritten.
  - **Precedence with `supply_v`.** Parameters are evaluated first, instance
    `alter`s after: if a parameter sets a source's default, an explicit
    `corners.supply_v` override of that source wins its value. Note that
    `alter` addresses *instances* and never updates a `.param` definition —
    that is what this axis is for.
  - **Identity.** `params` (the normalized scalar map, integral values as
    integers) is echoed on each corner report and in `_explicit_points`. For a
    parameter point the `corner_id` gains `/params:<pairs>` before any
    `/mc<N>` suffix: pairs are sorted lowercase names joined by commas, each
    `name=value`, values in the shortest round-trip decimal (integral values
    as integers, negative zero as `0`) — never three-decimal rounding or
    voltage units. Example: `default/novdd/27C/params:code=1,rload=2000`.
    A request with no (or an empty) `params` keeps its legacy IDs and slugs
    byte-for-byte. The artifact directory of a parameter point is the existing
    PVT slug plus `_params_` plus the full hexadecimal SHA-256 of the UTF-8
    `corner_id` (MC suffix included), so close-valued parameters never share a
    directory. Checkpoint identity uses the complete ID and the existing
    request fingerprint.
  - **Support.** ngspice on `local`/`local-parallel`, and on `remote`/`batch`
    **with an updated worker image** (one built from a `klt` that includes
    `corners.params`). `engine: "xyce"` with `corners.params` fails before
    execution, and `--op-lint` refuses a corner that carries parameters.
    An older worker silently ignores the unknown field, and the runner
    version check compares only major.minor, so it cannot tell such an image
    apart. `klt sim` therefore checks the answer: every returned corner must
    echo the dispatched `corner_id` and `params`, otherwise the request fails
    with error code `params_unsupported_worker` and reports no result. Update
    the runner image, or use a local backend.
- **`exclude`** (`array<object>`, optional) — partial corner specs
  (`process`, `temperature_c`, `supply_v`, and/or `params`) dropped from the
  expansion, for sparse matrices. `exclude[].params` is an exact-match subset
  of a point's `params` map (names case-insensitive, values compared
  numerically), ANDed with the other selector keys; naming a parameter the
  request does not sweep is a `SimError`.

Expansion is deterministic and odometer-style: process outermost, supply
next, temperature next, parameter row innermost — the same corner list, same
order, every run against the same request.

### Explicit unit list: `corners.units`

The crossed axes above always produce a full cross product. For adaptive
refinement (a few extra samples at specific coordinates) or a dependent sweep
(a source level scanned `0..supply` *per* supply value), pass
**`corners.units`** instead: a non-empty array of
`{"process"?, "temperature_c"?, "supply_v"?: {rail: number}}` records, each
executed exactly as given with no crossing
([#2993](https://github.com/2AMLogic/klayout-tools/issues/2993)).

```json
"corners": {
  "units": [
    {"process": "tt", "temperature_c": 27, "supply_v": {"vdd": 1.8, "vin": 0.90}},
    {"process": "tt", "temperature_c": 27, "supply_v": {"vdd": 1.8, "vin": 0.95}},
    {"process": "ss", "temperature_c": 125, "supply_v": {"vdd": 1.62, "vin": 0.81}}
  ]
}
```

- Omitted fields collapse to the crossed form's defaults (`process` none,
  `supply_v` `{}`, `temperature_c` 27); `process` accepts the same string or
  bundle forms; `supply_v` values are **scalars**, not arrays.
- `corner_id`/slug are unchanged, so a unit naming the same coordinates as a
  crossed point has an identical `corner_id` (`--resume` checkpoints and
  `pre_completed` reuse work across both forms). `corners[]` in the report is
  in record order, and `monte_carlo` clones each unit as usual.
- Mutually exclusive with `corners.process` / `supply_v` / `temperature_c`
  and with `exclude`; an empty list, a non-object record, an unknown record
  key, an array-valued `supply_v`, or two records with the same `corner_id`
  (supply is formatted to 3 decimals, so 0.1004 V and 0.1001 V collide) is a
  `SimError`.
- Works on every backend, including `remote`/batch (the worker re-expands the
  same list).

### Per-section corner libraries

gf180mcu's bundle above still reads every section from **one** file. Some
PDKs instead ship one corner library *per device family* — separate
MOS / capacitor / bipolar corner files, each with its own section names — so
a single named corner needs `.lib` cards against several different paths. A
`sections[]` entry may therefore name its own library
(issue [#2522](https://github.com/2AMLogic/klayout-tools/issues/2522)):

```json
"models": { "pdk": "somepdk", "lib": "libs.tech/ngspice/mos.lib" },
"corners": {
  "process": [
    {
      "name": "ss",
      "sections": [
        "ss",
        { "lib": "libs.tech/ngspice/moscap.lib", "section": "moscap_ss" },
        { "lib": "libs.tech/ngspice/bjt.lib", "section": "bjt_ss" }
      ]
    }
  ]
}
```

emits, in declaration order:

```
.lib <resolved models.lib> ss
.lib <resolved moscap.lib> moscap_ss
.lib <resolved bjt.lib> bjt_ss
```

- **Same resolution rule as `models.lib`.** A `lib` ref is joined against
  the resolved PDK variant directory when the request names a PDK
  (`models.pdk`/`models.pdk_root`), otherwise `$VAR`/`~` are expanded and a
  relative path is joined against the request file's own directory — see
  "Model library resolution" below.
- **Existence-checked before any corner is dispatched**, with a named error
  (`corner section library not found: <path>`, exit `1`), the same posture as
  `models.lib`'s own `model library not found`. It never surfaces as a
  per-corner ngspice parse failure deep inside a sweep.
- **Mixed forms are fine**, as above: a bare-string section and a
  per-section-lib object may appear in the same `sections` array. Bare
  strings keep binding to `models.lib`, byte-for-byte as before.
- **`models.lib` becomes optional** when every section of every selected
  corner names its own library — the case for a PDK with no all-device
  library to point it at. `environment.models_lib` then reports
  `{"path": null, "scope": "absent"}` with a null `models_lib_sha256`.
- **Provenance.** `environment.corner_section_libs` lists each distinct
  per-section library, in first-appearance order, as
  `{name, path, scope, sha256}` — normalised exactly like
  `environment.models_lib` (an out-of-repo PDK file reports
  `{"path": null, "scope": "external"}`), because `models_lib_sha256` alone
  no longer answers "which models produced this result". Present only when a
  request declares per-section libraries. A resumable sweep's checkpoint
  fingerprint keys on these hashes too, so editing one of those files
  invalidates the checkpoint exactly like editing `models.lib` does.
- **The same cards reach every deck** — the `fail_fast_probe` deck, the
  `--op-lint` deck, and the Xyce cross-validation deck all emit a corner's
  `.lib` cards from one shared code path, so a lint or an oracle run can
  never read different models than the sweep it accompanies.
- **Off-host backends resolve the refs on their own box.** `remote`/`batch`
  forward the refs **as declared** (this host's absolute paths would be
  meaningless there) and the remote host resolves them against its own
  `$PDK_ROOT`/request directory, exactly as it already resolves
  `models.lib`. Per-section libraries are *not* staged by default — same as
  `models.lib`, and unlike the netlist's `.include` closure (set
  `options.stage_model_inputs: true` to ship them; see "Staging the
  request's model inputs" above) — so express
  them PDK-relative (`models.pdk` + a relative `lib`) rather than as an
  operator-local absolute path if the request may run off-host. The
  *orchestrator* still resolves and existence-checks every ref locally to run
  the preflight above, so it needs its own readable copy even when the corners
  themselves run elsewhere — exactly as `models.lib` already behaves.

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
- **`monte_carlo.seed_scope`** (string, optional, default `"per_corner"`) —
  what keys each sample's seeds besides `seed` and `sample_index`:
  `"per_corner"`, `"shared"` or `"point"`. See "Seed scope" below.
- **`monte_carlo.quantiles`** (array\<number\>, optional) — percentiles in
  `[0, 100]` reported per measurement. Defaults to `[5, 50, 95]` (the
  median plus the symmetric tails). Duplicates are dropped; declaration
  order is preserved and becomes the response key order.
- **`monte_carlo.k_sigma`** (number, optional) — the sigma multiple `k` for
  the run-wide limit-window check (see "Monte Carlo statistics"). Omit for
  no window check. Overridable per measurement with
  `measurements[].k_sigma`.
- **`monte_carlo.window_scope`** (string, optional) — which population's
  `mean ± k*sigma` window gates status: `"pooled"` (default) or
  `"per_corner"` (see "Pooled vs. per corner"). Anything else is an
  application error.

`monte_carlo` is orthogonal to `corners.*`: the PVT axes still select
*which* process/supply/temperature points are simulated — including an
already-supported mismatch-enabled `.lib` section like sky130's `tt_mm`
(see "Corner axes" above) — while `monte_carlo` asks for each of those
points to be re-run `n` times with a different seed. A request combining
both expands to `corner_count * monte_carlo.n` total runs.

**Seed contract.** `monte_carlo.seed` makes the derived per-sample seed
integers reproducible: the same `seed` always derives the same per-sample
seed values, in any process, on any machine — the derivation is
SHA-256-based, never Python's salted built-in `hash()`. Those integers are
stable; the random *draws* ngspice produces from them are not part of the
contract. Reproducing a given sample's draws requires the same engine build,
netlist, model inputs and request. Identical seeds do not guarantee identical
draws across ngspice versions or builds (a different build can draw a
different sequence from the same `.options seed=`, shifting measurements by
far more than run-to-run noise on a fixed engine), nor bit-identical
numerical measurements. This matters when comparing local, `--backend batch`
and `--backend remote` results, which may run different ngspice builds:
compare `environment.engine_version` first. A matching version string is
useful provenance but does not uniquely identify every build, and a
mismatch means per-sample differences are not evidence about the circuit.
Each sample derives two independent components — `process_seed`
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

**What the seed contract does not cover (issue #2963).** It governs
`agauss`/`gauss`/`random()`-style draws only. ngspice's transient-noise
sources (`trnoise()` / `trrandom()` on `V*`/`I*` sources) are not seeded by
`.options seed=`; their generator defaults to the process id. Re-running the
same request redraws their noise (see also
[#2490](https://github.com/2AMLogic/klayout-tools/issues/2490), per-engine-build
reproducibility, and [#2937](https://github.com/2AMLogic/klayout-tools/issues/2937),
the combined seed), and samples run in separate batch jobs or hosts can share
a noise stream. When the netlist (or its scanned `.include` closure, including
`.subckt` bodies) declares such a source, `environment.monte_carlo` gains
`unseeded_sources` (sorted `[{"name", "keyword"}]`), `unseeded_sources_note`,
and, if part of the closure could not be scanned,
`unseeded_sources_not_scanned`; a warning is also printed to stderr. Requests
without such sources keep their `environment.monte_carlo` shape unchanged.
Seeding these sources per sample is not implemented. For what noise such a transient does and does not contain, see
"Transient noise coverage" under "Engine".

**Seed scope (paired and split-stable draws).** By default
(`seed_scope: "per_corner"`, also what you get when the field is absent) a
sample's seeds are keyed on the point's *position* in this request's expanded
corner list, so sample *k* is a different die at every corner, a single-point
request always uses position 0, and reordering axes or splitting a grid
across requests changes the draws. Existing reports reproduce bit-for-bit.
Two opt-in scopes change only that key:

| `seed_scope` | Seeds keyed on | Use it for |
|---|---|---|
| `per_corner` (default) | position in the expanded corner list | unchanged behaviour |
| `shared` | `sample_index` only | paired draws: sample *k* is the same die at every corner (calibrate at one condition, measure at another) |
| `point` | the point's own identity (process / sections, supplies, temperature) + `sample_index` | split-stable draws: any partition or reordering of a grid, or a single-point re-run, draws the same samples as the full grid |

Under every scope the negative control below still holds (the axis not in
`vary` stays pinned). The scope is echoed as `environment.monte_carlo.seed_scope`
and on each sample's `corners[].monte_carlo.seed_scope`, only when the request
declared it. An unknown value is rejected before any corner runs. A
`resume` checkpoint written under one scope is not reused under another.
For `--backend remote`/`batch` a non-default scope is resolved client-side and
the already-derived seeds are shipped to the job (the way a fleet shard is),
so a runner that predates the field cannot silently drop it and fall back to
`per_corner`.

**Deterministic negative control.** A seed component only varies across
samples when `monte_carlo.vary` actually asks for that axis — requesting
`vary: "process"` derives a different `process_seed` per sample but the
*same* `mismatch_seed` for every sample of a given corner, and vice versa
for `vary: "mismatch"`. Sigma=0 holds for the reported seed fields and for
decks/netlists that consume `mc_mismatch_seed` directly. It does **not**
hold for a deck that draws mismatch from ngspice's own RNG
(`agauss`/`gauss` in `.param` expressions, as the gf180mcu deck does):
ngspice receives only the combined `.options seed=<rndseed>`, which is
derived from both components and so still changes per sample under
`vary: "process"`. For such decks the working control is to switch the
deck's own mismatch off (e.g. gf180mcu `sw_stat_mismatch=0`) and confirm
sigma=0. The control guards against a
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
`mosfet`, `res_xpoly_1p1000pl` → `resistor`; gf180mcu's own passive names
`ppolyf_*`/`npolyf_*`/`nplus_*`/`pplus_*`/`rm<N>` → `resistor` and
`cap_mim_*` → `capacitor`, both reported `active: false`), each stating whether that
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

The family list is built from the netlist's whole `.include`/`.inc`
closure (the same set recorded under `environment.netlist_closure`), not only
the top-level file, so a wrapper testbench reports the families its included
flat netlist instantiates (issue #2928). When the closure is more than the
lone top-level file, or part of it could not be scanned, an additive
`environment.monte_carlo.family_mismatch_scan` object discloses
`scanned` (repo-relative file paths) and `not_scanned` (`{target, reason}`:
`env_var`/`pdk_root` directives left to the executing host, `unreadable`, or
`unresolved: ...` when the closure could not be resolved and only the
top-level file was scanned). Families defined only behind a user `.subckt`
call whose body is in an unscanned file are not expanded.

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

### Per-instance mismatch selection

`monte_carlo.instances` (optional, issue #2995) turns local mismatch on for a
subset of instances, so a spread can be attributed to a device group without a
hand-written netlist wrapper:

```json
"monte_carlo": {
  "n": 200, "seed": 7, "vary": "mismatch",
  "instances": { "include": ["xm1*", "xm2*"], "exclude": ["xm1_tail"] }
}
```

`include` and `exclude` are case-insensitive globs over **top-level `X`
(subcircuit) instance names** of the request netlist. An instance is
randomised when it matches `include` (or `include` is empty) and does not match
`exclude`. At least one list must be non-empty. klt wraps every other `X`
instance in a generated one-instance subcircuit that sets the PDK's mismatch
switch to `0` locally (`mc_mm_switch` for sky130, `sw_stat_mismatch` for
gf180mcu); the instance line, including `m` and every other parameter, moves
*inside* the wrapper verbatim, so nothing is forwarded across the extra
hierarchy level. The netlist you wrote is not modified; a gated copy runs.
Hierarchical probes of a wrapped instance gain one level
(`xm1.xm1_klt`).

**Verified on ngspice 46 with sky130A (2026-10-16, #3442).** Wrapping every
`X` instance under `tt_mm` removed all spread (8/8 samples exactly `0.0`, the
same as a mismatch-off run), so the PDK model cards do resolve the local
`mc_mm_switch=0` in the wrapper's scope. With `xm1`/`xm2` randomised the
`vdiff` stddev was 10.7 mV, and `m` is carried inside the wrapper verbatim.
ngspice 42 has not been compared (unverified).

Refused up front with a `SimError`: a glob that matches no `X` instance (or
only non-subcircuit elements), a hierarchical glob (`.`/`/`), `vary:
"process"`, a `models.pdk` without a known switch, and the `remote`/`batch`
backends. Instances inside user `.subckt` bodies and in `.include`d files are
not gated individually.

The response adds `environment.monte_carlo.instance_mismatch`:

```json
{
  "switch": "mc_mm_switch",
  "include": [], "exclude": ["xm1"],
  "instances": [
    { "instance": "XM1", "model": "sky130_fd_pr__nfet_01v8", "randomised": false },
    { "instance": "XM2", "model": "sky130_fd_pr__nfet_01v8", "randomised": true }
  ],
  "negative_control": false
}
```

`negative_control: true` means every `X` instance was excluded: that run should
reproduce the mismatch-off run under the same seed, so diff it against one to
catch a wrapper that drops a parameter. The report states which instances were
gated, not that the PDK model responded to the switch. Whether the remaining
instances draw the same values as an ungated run under the same seed depends on
the engine's random-stream ordering and is not guaranteed by klt. Worked
example: `examples/sim/request-monte-carlo-instances.json`. Key names and entry
shape are intended to stay consistent with the injected-mismatch proposal in
#2911.

**Design-owned (injected) mismatch — `monte_carlo.injected`.** When a PDK's
resistor/capacitor model has no per-instance mismatch hook (the
`active: false`/`null` families above), declare the mismatch the design
injects itself and let `klt sim` own the draws and report them:

```json
{
  "monte_carlo": {
    "n": 300, "seed": 20260801, "vary": "mismatch",
    "injected": [
      { "pattern": "xr*", "parameter": "m", "relative_sigma": 0.02,
        "distribution": "gaussian" }
    ],
    "injected_scale": 1
  }
}
```

- **`injected[]`** — each entry has exactly one of `instance` (exact element
  name) or `pattern` (`fnmatch` glob over the netlist closure's element
  names, including those inside `.subckt` bodies; matching is
  case-insensitive and hierarchy is flat), plus `parameter` (a label that
  names the param), `relative_sigma` (a number in `(0, 1)`), and an optional
  `distribution` (`"gaussian"` default, or `"uniform"` with the same
  standard deviation).
- **`injected_scale`** (number ≥ 0, default `1`) — global factor on every
  sigma: `0` turns the injection off (a negative control: every draw is
  applied as exactly `1.0`), `3` inflates it (a positive control).
- **How it is applied.** `klt` does not rewrite your netlist. Per sample it
  writes `.param mc_inj_<instance>_<parameter>` (the multiplier
  `1 + relative_delta`) and `..._delta` (`relative_delta`) into the deck —
  names lower-cased, runs of non-alphanumerics as `_`, e.g. `xr1` + `m` →
  `mc_inj_xr1_m` — and the netlist body consumes them
  (`XR1 a b rh m='mc_inj_xr1_m'`). A declaration whose selector matches no
  instance, or whose param the netlist closure never references, is
  rejected before any corner runs, so an injection is never reported live
  while nothing consumes it. Other validation errors: unknown keys, a
  non-positive or `≥ 1` sigma, an unsupported distribution, a duplicated
  `(instance, parameter)`.
- **Draws.** Each draw is a SHA-256 function of the sample's
  `mismatch_seed`, the instance and the parameter — klt-recorded values, not
  ngspice's `agauss` stream — so the same request reproduces the same draws
  and changing `n` (or other axes) never shifts another sample's draws.
  `vary: "process"` pins `mismatch_seed`, so injected draws are identical
  across samples (the same deterministic negative control as above).
- **Per-sample record.** Each `corners[].monte_carlo` gains an additive
  `injected[]` of `{instance, parameter, draw, relative_delta, value}`
  (`draw` is the unit-variance draw; `relative_delta = injected_scale *
  relative_sigma * draw`; `value = 1 + relative_delta`).
- **`family_mismatch`.** Injected families are appended (PDK-table entries
  are unchanged) as `{family, active, source: "injected", parameter,
  relative_sigma, distribution, instances, note}`; `active` is `true` only
  when `injected_scale > 0` and `vary` includes mismatch. The block appears
  for an injected request even when `vary` is `"process"`. The environment
  also echoes `injected_scale`. If the PDK's own mismatch for the same
  family is active too, the entry's `note` says the two compose.
- **Backends.** The draws ride the existing per-point seed wire, so the
  `batch`/`remote` fleets carry them unchanged; validation against the
  netlist closure happens on the orchestrating host.

Worked example: `examples/sim/request-monte-carlo-injected.json` (with
`testbench-mc-injected.spice`).

## Retrying a non-converged unit (`options.retry_on_convergence_error`)

Issue #2973. One unit of a large `monte_carlo` request can end `status: "error"`
from a purely numerical failure (ngspice "time step too small"). Resubmitting the
whole request is expensive and the rest of the population is already good, so a
request can opt into a per-unit retry:

```json
{ "options": { "retry_on_convergence_error": { "max_attempts": 1, "reltol": 0.003, "method": "gear" } } }
```

`true` is shorthand for the object above; `false`/absent (the default) leaves
every request and report unchanged. Object keys: `max_attempts` (integer >= 1),
`reltol` (number in `(0, 1)`), `method` (`"trap"` or `"gear"`). An unknown key or
a bad value is refused with an application error before any unit runs.

- **Trigger.** Only a unit whose first attempt is `status: "error"` *because of* a
  `nonconvergence` diagnostic (plus the missing-measurement / missing-vector
  errors that follow from it). `singular_matrix`, `netlist`, `timeout`,
  `engine_killed` and every other error class are never retried, and a
  `nonconvergence` ngspice recovered from by itself (a `pass`) is not either.
- **Same draw.** The retry re-runs the same unit, so `sample_index` and
  `monte_carlo.{seed,process_seed,mismatch_seed}` are unchanged; only an extra
  `.options reltol=<reltol> method=<method>` card is added to the deck.
- **Recorded.** The returned `corners[]` entry carries a `convergence_retry`
  diagnostic naming the card and whether the retry recovered; its `runtime_s` is
  the total across attempts. A recovered unit contributes to the Monte Carlo
  statistics and `coverage` like any other sample; one that still fails stays
  `error`.
- **Echo.** The resolved policy is reported as
  `environment.retry_on_convergence_error` (only when set).
- **Backends.** `local`/`local-parallel` apply it directly. `remote`/`batch`
  forward the request's `options` verbatim, so the runner applies it itself (the
  runner's `klt` must support the option). `engine: "xyce"` refuses it.

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
    "inconclusive": 0,
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
      { "corner_id": "tt/1.800V/27C", "n": 300, "errored": 0, "inconclusive": 0, "mean": 1.20117, "...": "..." }
    ]
  }
}
```

| Field           | Type                | Description                                                                                                                          |
| --------------- | ------------------- | ------------------------------------------------------------------------------------------------------------------------------------ |
| `n`             | integer             | Samples that produced a usable number **and** were trusted enough to grade — the population every statistic below is computed over.    |
| `errored`       | integer             | Samples whose value was unextractable (`null`) and were therefore excluded. |
| `absent`        | integer             | Issue #2925. Present only for a measurement declared `optional: true`: samples that legitimately produced no value (status `"absent"`). Excluded from every statistic and **not** counted in `errored`; `n + errored + inconclusive + absent` is the total. |
| `inconclusive`  | integer             | Issues #2492/#2493. Samples the run produced a number for but will not vouch for — drawn from a corner graded `"inconclusive"` by `options.fail_on_diagnostic`, or with a value outside a declared `options.node_voltage_bounds`/`plausible_range`. Excluded from every statistic in this block (including `sigma_window`) and counted here instead — the same treatment `errored` gets, for the analogous reason. Always `0` for a request that opts into neither. `n + errored + inconclusive` is the total sample count for this measurement. |
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

By default only the pooled window gates `measurements[].status` (and so the
envelope `status` and exit code), so a corner whose own population violates
the window passes if the pooled union does, and a pooled window inflated by
between-corner spread can fail although every corner passes. Declare
`monte_carlo.window_scope: "per_corner"` to grade every `by_corner[]` window
instead: the measurement fails if **any** corner's `sigma_window.status` is
`fail`, and the pooled window is still reported but no longer gates. The
measurement's `monte_carlo` block then also carries an additive
`failing_corners` array (the `corner_id`s whose window failed; `[]` when none)
— present only when `window_scope: "per_corner"` was declared, so default
responses are unchanged. `error` and `inconclusive` still outrank a window
`fail`, and a corner with no window (`sigma_window: null`, e.g. no `k_sigma`
declared) never fails the measurement. For a single-corner request both
scopes agree.

## Model library resolution (via `klt pdk`)

`models` resolves through the same discovery/resolution library that backs
`klt pdk` (never a hand-rolled path) — two supported shapes:

```json
{ "models": { "pdk": "sky130A", "lib": "libs.tech/ngspice/sky130.lib.spice" } }
```

Resolved via [`klt pdk find`](pdk.md)'s search order; `lib`, when relative, is
joined against the resolved variant directory. Optional `models.pdk_root`
pins the search the same way `klt pdk find --pdk-root` does.

**The resolution order, explicitly** (first match wins; the winning step is
reported as `provenance.pdk.source`):

| # | Candidate root | Notes |
|---|---|---|
| 1 | `models.pdk_root` | Pins the root and **disables** steps 2–4 entirely. |
| 2 | `$PDK_ROOT` | The invoking process's environment. A `$PDK_ROOT` that holds no matching install is skipped, falling through to step 3. |
| 3 | `~/.ciel`, then `~/.volare` | The ciel/volare stores, in that order — **`~/.ciel` first**. |
| 4 | `/usr/local/share/pdk`, `/usr/share/pdk`, `~/share/pdk` | Conventional open_pdks install prefixes, in that order. |

`models.pdk` (or `$PDK`) selects **which variant** within whichever root
wins; it does not reorder the roots.

**More than one install of the same variant is an ordinary host state**, not
a misconfiguration: a machine that has followed the tooling's own migration
carries both a `volare`-managed and a `ciel`-managed `sky130A` — two
different `open_pdks` builds, whose `libs.tech/combined/continuous/
models_fet.spice` (the FET cards a mismatch Monte Carlo draws from) need not
be byte-identical. Step 3 above then silently prefers `~/.ciel`, and a
campaign that verified `~/.volare` against a pinned `open_pdks` commit can
produce a record naming that pin with a simulation behind it that read the
other build. Since issue
[#2564](https://github.com/2AMLogic/klayout-tools/issues/2564) that
substitution is **reported rather than discarded**:

- **stderr** carries a one-line warning naming the absolute root actually
  read, every root skipped, and the remedy:

  ```text
  klt: warning: 2 installs provide PDK variant 'sky130A'; read
  /home/u/.ciel (search root: ~/.ciel), skipped /home/u/.volare (search
  root: ~/.volare). Resolution is first-match-wins, so a pinned open_pdks
  build may not be the one this run read -- pin the root explicitly
  (--pdk-root, or a request's models.pdk_root) to disable the search and
  choose deliberately.
  ```

- **`--format json`** records the skipped installs on
  `provenance.pdk.ambiguous_sources` — the path-free search-order labels
  (`["search root: ~/.volare"]`), never the absolute roots, which would bake
  the resolving machine's home directory into committed evidence. The key is
  **absent** when resolution was unambiguous, so an ordinary run's
  `provenance` is unchanged.

- **`--format text`** echoes the resolved PDK on its own line beside
  `models_lib`, plus a `WARNING` line when the variant was installed more
  than once:

  ```text
  models_lib: <outside repo>
  pdk: sky130A open_pdks 0fe599b (via search root: ~/.ciel)
  pdk: WARNING: variant also installed under search root: ~/.volare -- resolution is first-match-wins; pin models.pdk_root to choose deliberately
  ```

**Pinning `models.pdk_root` is the fix**, and it is quiet by construction:
step 1 disables the search, so there is no second candidate to be ambiguous
against. `$PDK_ROOT` narrows the search to one *first* candidate but does not
disable it, so a run pinned that way still reports any other install it
skipped.

```json
{ "models": { "lib": "$PDK_ROOT/sky130A/libs.tech/ngspice/sky130.lib.spice" } }
```

The spike's literal shape, for callers that already resolved `$PDK_ROOT`
themselves (e.g. via `eval "$(klt pdk env)"`). Env vars and `~` are expanded;
a relative path is joined against the request file's directory.

Both shapes apply verbatim to a `corners.process` bundle's per-section
`{"lib": ..., "section": ...}` refs (see "Per-section corner libraries"
above) — a per-section library resolves exactly as the `models.lib` it
stands in for would, and is reported in `environment.corner_section_libs`.

**Only the PDK-relative form is portable to `remote`/`batch`.** The runner
resolves `models.lib` on its own box against its own PDK root, so an absolute
host path (`/home/<user>/.volare/...`, or a `~/...` path) names a file that
does not exist there. On the `batch` backend such a request is refused before
any S3 write or instance launch
([#3296](https://github.com/2AMLogic/klayout-tools/issues/3296)): exit `1`,
`error.code` `models_lib_host_path`, naming each offending ref (`models.lib`
or a per-section `corners.process[].sections[].lib`) and, when the path sits
in a `<variant>/libs.tech/...` tree, the replacement
`{"pdk": "<variant>", "lib": "libs.tech/..."}`. Not refused: a PDK-relative
`lib`, a `$PDK_ROOT/...` form (the runner expands it), and any request with
`options.stage_model_inputs: true` (the library ships with the job).
`local`/`local-parallel` accept absolute paths unchanged.

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

## Plausibility bounds (`options.node_voltage_bounds` / `measurements[].plausible_range`)

Issue #2493. ngspice's Newton solve can converge onto a numerically valid but
physically implausible operating point — a compact model evaluated far
outside its fitted range, or an extrapolation branch — without emitting any
log-level diagnostic: this is a *silent* failure mode, distinct from
everything "Failure classification" below catches, because there is no
distinguishing log text for `_classify_diagnostics` to match. A `.meas` card
sampling that node still returns an ordinary-looking numeric value (observed:
orders of magnitude outside the deck's own declared supply rails), which the
ordinary `limits` comparison then grades exactly as it would a legitimate
result — turning a non-physical solve into an ordinary-looking `pass` or
`fail`, purely depending on which way the declared `limits` bound happens to
face.

Two **optional, opt-in** declarations close this gap by giving `klt sim` a
plausibility range to check a measured value against, *before* the ordinary
`limits` comparison ever runs:

- `measurements[].plausible_range: {min, max}` — per-measurement, either
  bound optional but at least one required. **Unscoped**: applies to
  whatever measurement it is attached to, regardless of `unit` — an explicit
  per-measurement declaration is always the caller's deliberate act.
- `options.node_voltage_bounds: {min, max}` — a run-wide default. **Voltage-
  scoped**: it only auto-applies to a measurement that declared `unit: "V"`
  (there is no measurement "kind" tag elsewhere in the request schema to
  gate on otherwise) — a measurement with any other `unit` (or none) never
  inherits it, though it can still opt in explicitly via its own
  `plausible_range`. A measurement's own `plausible_range` always wins over
  the inherited default when both are declared.

A value outside its resolved plausible range is graded `"inconclusive"` — a
status distinct from both `"pass"` and `"fail"` — rather than being handed to
the ordinary `limits` comparison at all (`margin` is `null`: it is defined
relative to `limits`, which never ran), and the corner gains a
`severity: "warning"` diagnostic with `code: "implausible_solution"` naming
the bound it violated. This is deliberately **not** the same thing as a
`"fail"`: a `"fail"` means the simulator produced a trustworthy number and
the design missed a declared limit; `"inconclusive"` means the number itself
cannot be trusted enough to say either. It outranks `"fail"` in every
aggregate (`corners[].status`, `measurements[].status`, the top-level
`status`), the same way `"error"` outranks both — see "Failure
classification" below for the full precedence table. The top-level response
also counts the corner in `inconclusive` (alongside
`passed`/`failed`/`errored`, always present, `0` for any request that
declares neither a plausibility bound nor `options.fail_on_diagnostic`) and,
correspondingly, `klt sim`'s CLI exit code `4` (the same code as
`"error"`/`"not_checked"` — see "Exit codes" below): an implausible solve is
exactly as untrustworthy for grading as a corner that errored outright, so
it must never exit clean.

A `monte_carlo` sample set excludes implausible samples from every pooled
statistic (`mean`/`stddev`/`min`/`max`/quantiles/`sigma_window`) the same
way it already excludes unextractable (`errored`) ones — see "Monte Carlo
statistics" above — so a single implausible outlier cannot silently skew the
sigma window's verdict for the rest of the (trustworthy) sample. They are
counted in that block's `inconclusive` field.

#### One status, two reasons

`"inconclusive"` is **one** status with two ways in, not two statuses:

| Reason | Declared by | Diagnostic `code` | Graded onto |
| --- | --- | --- | --- |
| The solve narrated trouble the caller does not trust | `options.fail_on_diagnostic` (issue #2492) | `inconclusive` | the whole corner (each measurement keeps its own `pass`/`fail`) |
| The solve converged somewhere physically impossible | `measurements[].plausible_range` / `options.node_voltage_bounds` (issue #2493) | `implausible_solution` | the measurement (and so, by precedence, its corner) |

Both say exactly the same thing about the report — *a number exists and must
not be graded against `limits` as if it were a result* — so they share one
status token, one top-level count, one Monte Carlo exclusion field and one
slot in the `error > inconclusive > fail > pass` precedence. Two
near-synonymous status tokens would have forced consumers to learn both, a
five-term sum invariant, and an arbitrary precedence between two claims
neither of which is stronger than the other. The *reason* is not lost: it is
in the marker diagnostic's `code`, which `diagnostic_counts.by_code` (issue
#2491) already rolls up per code, so the per-reason breakdown is derived
rather than duplicated.

A `klt eval` gate citing such a report reports `exit_code: 4` too, not `3` —
see [`eval.md`](eval.md)'s `gates[].exit_code` row.

Both declarations are **validated up front**, before the first corner is
dispatched: each must be an object declaring at least one of `min`/`max`,
each bound must be a number (a JSON boolean is rejected), and `min <= max`.
Unlike `limits` — which is read defensively and silently scores a typo'd key
as no bound at all (see "`coverage`" below) — a malformed plausibility bound
fails the whole request with exit `1`. A bound exists specifically to
*reclassify* an otherwise-graded value, so one that silently never fired
would reintroduce the exact gap it was declared to close.

**Fully backward compatible**: neither field has a default, so a request
that declares neither behaves byte-for-byte as it did before this issue —
every measurement is graded via the ordinary `limits` comparison exactly as
today.

Example — a supply-rail-derived bound catching the false-fail scenario above
(a 1.8 V rail whose internal node reads `142.7` V, a numerically valid but
physically impossible solve, on a `limits` bound that would otherwise have
graded it `"fail"`):

```json
{
  "measurements": [
    {
      "name": "vinternal",
      "spice": ".meas tran vinternal FIND v(internal_node) AT=5u",
      "unit": "V",
      "limits": { "min": 0.0, "max": 1.9 }
    }
  ],
  "options": {
    "node_voltage_bounds": { "min": -0.5, "max": 2.5 }
  }
}
```

```json
{ "name": "vinternal", "value": 142.7, "unit": "V", "status": "inconclusive", "margin": null }
```

Without `options.node_voltage_bounds` (or a per-measurement
`plausible_range`) declared, the identical `142.7` reading grades
`"fail"` — a real accuracy-bound miss on its face, but actually an artifact
of grading a non-physical solution as if it were real. Declaring the bound
is what tells `klt sim` the difference.

## Failure classification: from the log, never the exit code

`ngspice -b` reliably exits `0` even when a `.meas` fails or the matrix is
singular (verified empirically against ngspice 46 while building this
command — see the spike's "Failure signalling" survey row). Every corner's
`diagnostics` are classified from its own log file's text:

| `code`             | Detected from                                                              |
| ------------------ | --------------------------------------------------------------------------- |
| `singular_matrix`  | A `Warning: singular matrix` line.                                          |
| `timestep_too_small` | ngspice's own transient abort, `Timestep too small; time = <t>, timestep = <dt>: trouble with node "<node>"` (issue [#2918](https://github.com/2AMLogic/klayout-tools/issues/2918)). Carries additional `time_s` (number \| null: simulation time of the abort), `timestep_s` (number \| null: the timestep ngspice had shrunk to) and `node` (string \| null: the node/branch ngspice blamed, e.g. `"vctl#branch"`) fields — each `null` when ngspice's line omitted that part — repeated in `message`. Surfaced from the log the classifier already reads, so `keep_artifacts` is not needed to see it. Always `error`, **never** downgraded by the stepping-recovery rule below: it is a terminal abort, not recovery narration. Previously such a run surfaced only as one `measurement` diagnostic per `.meas`. See "Known limitation: `trnoise()` with non-DC control sources" below for the commonest trigger. |
| `nonconvergence`   | Iteration-limit / gmin-stepping / source-stepping / time-step-too-small text (Xyce's `Time step too small`; ngspice's own `Timestep too small` abort is `timestep_too_small` above). |
| `missing_include`  | ngspice's own `Error: Could not find include file ...` — a `.include`/`.inc` target that did not resolve. Everything downstream of it (missing devices, then `.meas` cards finding no vector) reads like a broken circuit, so it gets its own code rather than surfacing only as measurement failures ([#2485](https://github.com/2AMLogic/klayout-tools/issues/2485)). For an off-host backend this is now unreachable by construction — the include closure is staged, or the submit is refused (see "Off-host backends stage the netlist's `.include` closure" above). |
| `netlist`          | A top-level `Error:` line naming a syntax/unknown/undefined/parse/subckt problem. |
| `model_bin_range`  | ngspice's own `Error: could not find a valid modelname` — undiagnostic on its own; enriched with the netlist's likely culprit instance (largest `w`, or `w * nf` when fingered, without `m=`) when one is found. See "Model bin-range diagnostic" below. |
| `unsupported_model_level` | ngspice's `Only MOS device levels ... are supported in this binary` (e.g. ASAP7 BSIM-CMG `level = 72` without an OSDI module); reported as `prerequisite missing: ...` and suppresses the follow-on `model_bin_range`. See "ASAP7 FinFET (BSIM-CMG) simulation". |
| `osdi_abi_mismatch` | `NGSPICE only supports OSDI v0.N but ...` — ngspice too old for the compiled module's OSDI ABI. |
| `osdi_load_failed` | `Error: Library <x>.osdi couldn't be loaded!` — the module is unreadable or not loadable by this ngspice. |
| `timeout`          | The per-corner `options.timeout_s` budget was exceeded; the process is killed. |
| `engine_killed`    | The engine process died from a signal the harness did not send (OOM killer, host reaper, reclaimed worker). `corners[].engine_killed_by_signal` names it (issue #2848). Always `error`; accepted by `options.fail_on_diagnostic`. A harness `timeout` is never reported this way. The message quotes the last ~20 lines of engine output (issue #3067). |
| `engine_aborted`   | The engine exited with a non-zero code (`corners[].engine_exit_code`) without a classifiable cause in its log. (A clean exit that wrote no log, or a non-zero exit with no log file at all, is reported as `unknown` with the exit status and output tail, issue #3016.) Always `error`; accepted by `options.fail_on_diagnostic`. The message carries the exit code and the last ~20 lines of engine stderr/stdout/log, also with `keep_artifacts: false` and on the batch backend. When a corner carries `engine_aborted` or `engine_killed`, its per-measurement `measurement ... produced no value` diagnostics are omitted (the measurement results stay `error`), so `diagnostics[0]` names the cause (issue #3067). |
| `measurement`      | A declared `.meas` produced no value (missing, not a `"fail"` — see below). |
| `measurement_absent` | Issue #2925. An `optional: true` measurement produced no value (`severity: "info"`; the measurement is `status: "absent"` and the corner is not graded `error` for it). Selectable in `options.fail_on_diagnostic` to grade a corner `inconclusive` when absence should not be accepted. See "Optional measurements". |
| `no_such_vector`   | ngspice's own `Error: no such vector as ...` — emitted *alongside* `measurement` when the missing signal's name never resolved to a vector at all (issue #2521). See "Saved signal set (`save all`)" below for what this can and cannot tell you. |
| `step_failed`      | An `analysis_steps[]` step's analysis produced no plot of its own (it failed, or the run ended before it) — read from `klt`'s own step markers in the log, never inferred from measurements. The step's own measurements are reported `value: null`/`status: "error"` under this one diagnostic (issue #2482). See "Several solves per corner" above. |
| `derived_input_unavailable` | A measurement computed from other results (a derived measurement, or a step `expr` reading an earlier step) was **not graded** because at least one of its inputs produced no value in this corner (issue #2482). Distinguishes "an input was missing" from a derived expression that itself failed, which is an ordinary `measurement` diagnostic. |
| `waveform_too_large` | `waveform.raw` exceeded `options.waveform_max_bytes`, so the `waveform.raw.json` conversion was skipped (issue #3211). Always `severity: "warning"`; measurements and `status` are unaffected. See "Waveform size guard" above. |
| `unknown`          | Anything else that prevented a trustworthy result (e.g. ngspice not installed/spawnable). |
| `budget_exceeded`  | The corner never started: the sweep's own `options.wall_clock_budget_s` was exceeded first. See "Wall-clock budget, orphan safety, and resume" above. |
| `orphaned`         | The corner never started: the launching process exited before its turn. See "Wall-clock budget, orphan safety, and resume" above. |
| `lost_shard`       | The corner's `hosts > 1` shard never returned (Epic #375). See "Fleet sharding" above. |
| `batch_job_failed` | The corner never started, or its report was never written: the `batch` backend's fleet job reached a terminal `failed` state with an exit code outside `klt sim`'s own `0`/`3`/`4` (see "Batch backend" above). |
| `batch_job_timeout` | The corner never completed: the `batch` job exceeded its own `timeout_seconds` on the fleet instance and was killed by 2am's harness. |
| `batch_poll_timeout` | The corner's result was never observed: `batch.poll_timeout_s` elapsed with the job still non-terminal, or the job never published a `status.json` within 1800s (the message says it never started). The job may still be running on the fleet — this is what the client knows, not a claim the run failed. |
| `timeout_budget_unreachable` | The corner never started: `options.fail_fast_probe`'s calibration probe measured a rate implying `options.timeout_s` cannot plausibly cover the full analysis window, so the whole grid was aborted before dispatch (issue #1694). Carries additional `reached_s`/`fraction` fields — an *estimate* of how far this corner would have gotten, derived from the measured rate, not a real per-corner recovery. See "Timeout-budget preflight" above. |
| `convergence_retry` | The record of an opt-in retry (`options.retry_on_convergence_error`, issue #2973): the unit's first attempt ended in a `nonconvergence` error, so it was re-run with tighter solver options. The message names the `.options` card used and whether the retry recovered. `severity: "warning"`; informational, it never changes the unit's status. See "Retrying a non-converged unit" below. |
| `inconclusive` | **Emitted, never selectable.** The marker this command attaches to a corner it graded `status: "inconclusive"` because `options.fail_on_diagnostic` named a code the corner emitted (issue #2492) — the message names those code(s), so the report says *why* a corner whose numbers look fine is not being reported as a pass. `severity: "warning"`. See "Grading a recovered diagnostic as inconclusive" below. |
| `implausible_solution` | **Emitted, never selectable.** The marker for the *other* route to `status: "inconclusive"`: a measurement's value fell outside its declared `options.node_voltage_bounds`/`measurements[].plausible_range` (issue #2493); see "Plausibility bounds" above. `severity: "warning"`, never `"error"`. |

**The `measurement` code's lookup is case-insensitive** (issue #2546): a
`.meas`/`.measure` card's own name is folded to lower-case by both engines
in their report output (ngspice already prints it lower-case; Xyce's own
upper-cased form is normalized by this command's parser), so `measurements[]`
matches a declared `name` (e.g. `"Vout"`) against the log's folded name
(`vout`) case-insensitively rather than requiring a byte-identical spelling.
The response still echoes the caller's own original `name` spelling — only
the internal lookup is case-folded.

The last two rows are the only ones produced by the **grading** step itself
rather than by a log classifier or a never-ran-corner path. Both are emitted
on a corner that is already being graded `inconclusive`, downstream of
`options.fail_on_diagnostic`'s code match, so listing either there could not
change any verdict — they are deliberately **not** accepted in that option,
and every other code in the table above is. They are also what tells the two
reasons apart in `diagnostic_counts.by_code` (issue #2491) once both land on
the same `status`.

**A `diagnostics` entry at `severity: "error"` makes that corner
`status: "error"`**, which always outranks everything else — `error` means
no trustworthy number exists; `fail` means the simulator produced a
trustworthy number and the design missed a limit; `inconclusive` (issues
#2492/#2493 — see "Grading a recovered diagnostic as inconclusive" and
"Plausibility bounds") means the simulator produced a number but the run
will not vouch for it, so it is not graded against `limits` as if it were a
result. `error` > `inconclusive` > `fail` > `pass` is the full precedence, at
both the per-corner `status` and the per-measurement `status` inside it.
Conflating `error`/`fail` is the specific defect this command exists to avoid
(see the spike's "Semantics and guarantees"); conflating `inconclusive` with
either is issues #2492/#2493's addition to the same discipline.

`singular_matrix`/`nonconvergence` are a documented exception, downgraded to
`severity: "warning"` (recorded, but non-fatal) rather than `"error"` when
the run recovered: ngspice's own gmin/source-stepping recovery narrates its
*intermediate* stepping attempts failing (`Warning: singular matrix`,
`... stepping failed`) even on a corner that goes on to complete
successfully — that narration alone is not evidence the analysis failed
(issue #205). The downgrade only applies when every one of the request's
`measurements[]` actually came back with a value for this corner *and*
ngspice's own `simulation(s) aborted` trailer is absent from the log; a
measurement that produced no value, or that trailer, keeps
`singular_matrix`/`nonconvergence` at `severity: "error"` and the corner at
`status: "error"`, exactly as before.

A waveform-capture-only request (no `measurements[]`, issue #3246) has no
measurement to confirm completion, so the captured waveform is the recovery
basis instead: for a `tran` analysis with `options.waveforms: true`, the
downgrade applies when the trailer is absent *and* the parsed waveform's last
time point reaches the `tran` stop time (the second `analysis.args` token). A
missing or truncated waveform, an unparseable `args`, or any non-`tran`
analysis keeps the diagnostic at `severity: "error"`. When the waveform was
the basis, the downgraded diagnostic's `message` says so. Declaring a dummy
completion measurement is no longer needed to get this behavior.
`options.fail_on_diagnostic` is unaffected (it matches on `code`).
`netlist`/`timeout`/`measurement`/`unknown`/`timestep_too_small` are never
downgraded.

A downgraded diagnostic still counts: `diagnostic_counts` (issue #2491, see
the top-level fields table above) rolls up every `corners[].diagnostics[]`
entry across the whole grid — by `code` and by `severity` — regardless of
whether the corner it came from ended up `pass`, `fail`, `error`, or
`inconclusive`. A `pass`ed corner with a recovered
`singular_matrix`/`nonconvergence` warning
still increments `diagnostic_counts.by_severity.warning` and
`diagnostic_counts.corners_with_diagnostics`, so a caller reading only the
top-level report — never `corners[]` itself — can still see that a
100%-`pass` grid recovered from engine trouble along the way.

`.meas` cards are also validated against ngspice's own supported analysis
types (`dc`/`ac`/`tran`/`sp`) before a request ever reaches ngspice: ngspice
has no `.MEASURE OP` — an operating point has no sweep variable for a
measurement to search over the way DC/AC/TRAN/SP do — so a `.meas op` card
(regardless of the request's own `analysis.kind`) is rejected up front with
an actionable `SimError` instead of failing deep inside an ngspice parse
error. Declare the quantity as a `measurements[].expr` expression instead (the
first-class answer — see "Measurements that are not `.meas` cards" above), or
use `analysis.kind: "tran"` with a short single-step transient and
`.meas tran ... at=<t>` to read back an operating-point-like value.

### Known limitation: `trnoise()` with non-DC control sources

A transient whose deck contains ngspice `trnoise()` voltage sources cannot
in practice also contain a control stimulus that is quiet and then changes
— a gated enable (`PWL`, one-shot `PULSE`, `EXP`, or a B-source on `time`)
or a finite supply ramp. ngspice (42 and 46 both reproduce, local or batch
backend alike) aborts at the first noise sample-clock edge with
`Timestep too small; time = <first sample edge>, timestep = 5e-23: trouble
with node "<some vsource>#branch"` (issue
[#2918](https://github.com/2AMLogic/klayout-tools/issues/2918)). This is an
engine limitation — the source breakpoints collide with `trnoise`'s
sampling — not something `klt sim` can work around. Observed:

- the identical deck with the `trnoise()` sources replaced by plain `dc 0`
  sources runs to completion;
- the identical deck with **zero-amplitude** `trnoise(0 ...)` (source still
  present) **still aborts**;
- a noise-free, always-oscillating dummy circuit added to keep the global
  timestep small does not help;
- the same noisy deck with **every control source a bare DC source** (the
  circuit self-starting from the `uic` all-zero state) **runs to
  completion**.

So stochastic transient noise and any stimulus that is not DC from t = 0
are mutually exclusive in practice: characterising a noisy circuit after an
enable edge or during a supply ramp is not currently supported. What `klt
sim` does guarantee is that the abort is *visible*: the corner (or Monte
Carlo unit) is `status: "error"` with a `timestep_too_small` diagnostic
carrying `time_s`/`node` (see the table above), and when the request's
netlist contains `trnoise` the message points here. Note also that ngspice
generates no device/resistor noise in a transient at all — only the
explicit `trnoise()`/`trrandom()` sources contribute; see issue
[#2985](https://github.com/2AMLogic/klayout-tools/issues/2985) for that
coverage disclosure.

**Counting aborted Monte Carlo units.** `statistics.<measurement>.errored`
stays a per-measurement count of unextractable samples, whatever the cause;
the per-cause split is already in the top-level `diagnostic_counts.by_code`
(issue #2491), so `diagnostic_counts.by_code.timestep_too_small` is the
number of units whose transient aborted, distinguishable from units whose
measurement merely produced no value (`measurement` without
`timestep_too_small`). No separate `statistics` counter is added for it.

### Grading a recovered diagnostic as inconclusive (`options.fail_on_diagnostic`)

The downgrade above is the right default — a recovered solve usually *is* a
good solve — but it is a judgement call the caller may not share. A corner
whose operating point only converged after gmin/source stepping is graded
exactly like a corner that converged on the first Newton pass: the
diagnostic becomes a `warning` and `status` comes purely from the
measurement-vs-`limits` comparison.

`options.fail_on_diagnostic` (issue #2492) is how a caller opts out of that
default for specific codes:

```json
{ "options": { "fail_on_diagnostic": ["singular_matrix", "nonconvergence"] } }
```

Any corner that emitted a listed `diagnostics[].code` is graded
**`status: "inconclusive"`** instead of `pass`/`fail`. The match is taken on
the `code` **before** the recovered-stepping downgrade runs, which is the
entire point: the "recovered" case is precisely the one this option exists
to let you distrust. `inconclusive` says *the solve did not convincingly
converge* — a distinct claim from `fail` (*the circuit missed its spec*) and
from `error` (*no number was produced at all*).

- **Precedence is `error > inconclusive > fail > pass`**, at the corner
  level and in the per-measurement rollup alike. `inconclusive` outranks
  `fail` for the same reason `error` already does: a limit miss computed
  from numbers the caller has declared untrustworthy is not a defensible
  claim about the design, so it is not reported as one. It sits *below*
  `error` because an errored corner produced no number at all — strictly
  less information than a number you decline to trust. (Like `error`, one
  inconclusive corner therefore masks a concurrent clean `fail` in the
  *aggregate* `measurements[].status`; `corners[]` and the top-level
  `failed`/`inconclusive` counts keep the per-corner detail.)
- **Top level**: the corners are counted in the `inconclusive` field (and
  `sim__corner__inconclusive_count`), and the aggregate `status` becomes
  `"inconclusive"` at exit `4` — whose documented meaning is already "the
  sweep is incomplete or **untrustworthy**" — unless some corner also
  errored, in which case `"error"` wins. This is a real top-level token, not
  a remapping onto `"error"`:
  [`docs/json-contract.md`](../json-contract.md) frames status/exit codes as
  *additive per verb* ("a command may ship a new code above `2` in a later
  release … the new code is a *new verdict*"), and `klt lvs` already ships
  this exact token at this exact exit code (issue #1370). Keeping it distinct
  is what lets a consumer tell "the simulator broke" (`"error"`) from "I do
  not trust these numbers" (`"inconclusive"`) without having to read the
  counts — and it keeps this option symmetric with the plausibility bounds
  above, which reach the same status by the other route. Gate on `status`,
  not the exit code (see "Exit codes" below).
- **Monte Carlo**: a sample drawn from an inconclusive corner is **excluded
  from that measurement's statistics entirely** — `mean`, `stddev`,
  `min`/`max`, `quantiles`, and therefore the `sigma_window` computed from
  them — and counted in the statistics block's own new `inconclusive` field,
  exactly the way an unextractable sample is counted in `errored`.
  `n + errored + inconclusive` always equals the number of samples drawn.
  Keeping distrusted samples in would let a draw the caller has explicitly
  disowned move the very mean and sigma the window verdict rests on. The same
  field also counts samples excluded by a plausibility bound — see "One
  status, two reasons" above.
- **Opt-in only.** Unset or `[]` (the default) is today's behaviour exactly:
  every count, status, and statistic is unchanged for a request that does
  not declare it. The new `inconclusive` fields are always present and `0`.
- **An unrecognised code is an application error** (exit `1`), not a silent
  no-op: a typo like `"singular-matrix"` that quietly matched nothing would
  hand you a report full of confident `pass` corners while you believed you
  had opted into stricter grading. Listing a *valid* code that this
  particular run never emits is fine and stays a no-op — that is a standing
  policy, not a mistake. The accepted vocabulary is the `code` column of the
  classification table above, minus the two emitted-only grading markers
  (`inconclusive`, `implausible_solution`).
- The `--fail-on-diagnostic <code>` CLI flag (repeatable) overrides the
  request field, and — like `--fail-fast-probe` — applies to the
  `local`/`local-parallel` backends; an off-host (`remote`/`batch`) shard
  re-invokes `klt sim` from the forwarded request document and reads
  `options.fail_on_diagnostic` there itself.

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
`save all` (issue #2521 — see "Saved signal set" above), `set
filetype=ascii`, and `write <rawfile>` before running the declared analysis;
the resulting ngspice ASCII rawfile is parsed into a small, documented
waveform JSON shape and referenced (not inlined) from the corner's
`artifacts.waveform`:

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

The rawfile stays ASCII (`set filetype=ascii`) and the waveform JSON shape is
unchanged (issue #3360): the rawfile is stream-parsed and the JSON written one
point at a time, so `klt sim`'s peak memory converting it does not scale with
rawfile size. A binary rawfile reader was considered and rejected because it
would change the `artifacts.raw` format.

### Capture-only runs (`options.capture_only`, [#3158](https://github.com/2AMLogic/klayout-tools/issues/3158))

A request with `options.waveforms: true` and an empty `measurements` list
runs every corner and writes the waveform artifacts, but checks nothing, so
it reports `status: "not_checked"` and exits 4 — the same code as a refusal.
A caller that post-processes the raw traces itself declares that intent with
`options.capture_only: true` (boolean, default `false`):

```json
{ "options": { "waveforms": true, "capture_only": true } }
```

Each corner that ran clean then counts its capture as checked work, the
report rolls up as `status: "pass"` (exit 0), and `coverage.capture_only` is
`true` — the field to read to tell a capture-only pass from a graded one.
The option is a request error (exit 1) unless `options.waveforms` is true
(or `--plot` is given), `measurements` is empty, and the value is a boolean.
It waives only "nothing was checked": a corner `error` (including
`nonconvergence`) still exits 4, and an empty corner matrix is still
`not_checked`. A request that does not set the option is unchanged. Coverage
is computed client-side after the corners are collected, so the option
behaves the same on the `batch` and `remote` backends.

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

## Stability margins at every crossing (`analyze_margins`, issue #3088)

`klayout_tools.sim_transfer.analyze_margins` reduces a complex loop gain
`T = numerator / denominator` to margins at **every** crossing. It is a pure
helper (no simulator, no request/response schema change); the `klt sim`
limit wiring lands once the `transfer` measurement form (#2923/#2724) is on
`main`. Existing first-crossing metrics from `analyze_transfer` are unchanged.

| Field | Meaning |
|---|---|
| `phase_margins[]` | `{freq_hz, direction, phase_margin_deg}` per 0 dB crossing (rise and fall). PM is `wrap(phase + 180)` into (-180, 180]: distance to the nearest -180 line, modulo 360, never from unwrapped phase. |
| `gain_margins[]` | `{freq_hz, direction, gain_margin_db}` per -180 deg crossing (mod 360); `-dB(|T|)`, so negative when `|T| > 1`. |
| `phase_margin_min_deg`, `gain_margin_min_db` | The scored values: the minimum over the lists. `null` plus a `reasons` entry (`no_zero_db_crossing`, `no_phase_crossing`, plateau) when absent -- no -180 crossing is neither a failure nor infinite margin. |
| `encirclement` | Clockwise encirclements of -1 (`2 x` net signed crossings of the ray left of -1), or `"indeterminate"`. 0 means stable for an open-loop-stable loop. |
| `band` | `f_min_hz`, `f_max_hz`, `mag_below_unity_at_top`, `low_freq_mag_db`, `min_samples_per_decade_near_crossing`, `crossings_bracketed`. |
| `ambiguity[]` | Why the band cannot support a pass: `top_not_below_unity`, `crossing_not_bracketed` (under 10 samples/decade or over 90 deg phase step at a crossing; tunable), `invalid_samples`, plateaus, `low_frequency_on_negative_real_axis`. |

`grade_margins(report, min_phase_margin_deg=, min_gain_margin_db=,
require_no_encirclement=)` returns `pass`, `fail` or `ambiguous`. A violation
among crossings actually found is a sound `fail`; `pass` needs an empty
`ambiguity`; otherwise `ambiguous`, which is never `pass` and never `fail`.
Corner status, rollup, exit code and schema treatment of `ambiguous` are part
of the follow-up wiring (tracked on #3088).

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
| `missing_node` | `error` | A declared I/O node (`op_lint.nodes`, or any `v(<node>)` reference inside the request's own measurements — `spice` cards and `expr` expressions alike) that does not appear in the netlist at all — the usual cause of a measurement that silently reads 0 V. |
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
| `engine`                 | string            | Engine selector. Defaults to `"ngspice"`; `"xyce"` (Sandia's Xyce, the cross-validation oracle — issue #2016) is implemented for DC/OP/TRAN analyses, process/temperature corners, and the local backends — see the "Xyce engine" section below. Any other value is an application error.                                                                                                |
| `backend`                | string            | Execution backend for the corner matrix. Defaults to `"local"` (runs corners sequentially in-process); `"local-parallel"` runs the same matrix across a bounded local worker pool; `"remote"` provisions an EC2 instance and runs it there; `"batch"` submits an S3 job contract to 2am's EDA batch fleet (see "Execution backends", "Remote backend", and "Batch backend" above). Overridable with the `--backend` CLI flag. |
| `remote.*`               | object            | Request fields for the `remote` backend (`region`, `key_name`, `ssh_key_path`, `launcher_cidr`/`launcher_cidrs`/`security_group_id`, `subnet_id`, `ssh_user`, `provider`, `spot`, `max_hourly_cost_usd`, `ssh_ready_timeout_s`, `ssh_timeout_s`, `ami_manifest`) — see "Remote backend" above. Only read/validated when `backend: "remote"` is selected. |
| `batch.*`                | object            | Request fields for the `batch` backend (`provision_script_path`, `bucket`, `jobs_prefix`, `region`, `profile`, `poll_interval_s`, `poll_timeout_s`, `capacity_wait_s`) — see "Batch backend" above. Only read/validated when `backend: "batch"` is selected. |
| `remote.hosts`           | integer           | Shard the expanded unit list across this many hosts and merge the per-shard reports. Defaults to `1` (today's single-host behaviour, byte-identical). Must be a positive integer, and (for `backend: "remote"`) no greater than the unit count. `local`/`local-parallel` shard in-process; `backend: "remote"` provisions a real `hosts`-instance EC2 fleet ([#906](https://github.com/2AMLogic/klayout-tools/issues/906)). Overridable with the `--hosts` CLI flag, same precedence rule as `backend`/`--backend`. See "Fleet sharding" above. |
| `models.lib`             | string            | Model library to bind process-corner `.lib` sections from. Required only when `corners.process` is set **and** at least one selected section reads from it (a bundle whose every section names its own `lib` needs none — issue #2522). See "Model library resolution" above. |
| `models.pdk`/`pdk_root`  | string            | Resolve `models.lib` — and any per-section `lib` — through `klt pdk find` instead of a literal path.                                                                    |
| `corners.process`        | array\<string \| {name, sections: array\<string \| {lib, section}\>}\> | Process-corner axis. Each entry is either a bare `.lib` section name (single `.lib` card) or a bundle object `{"name": str, "sections": [...]}` (one `.lib` card per section, in order). Each `sections[]` entry is a bare section name (read from `models.lib`) or `{"lib": str, "section": str}` naming its own model library for that section (issue #2522 — `$VAR`/`~` expand, relative refs resolve like `models.lib`; each must exist, checked before any corner runs, exit 1 otherwise). See "Corner axes" above. |
| `corners.supply_v`       | object            | Supply axis, keyed by voltage-source name (not a `.param`); arrays sweep together by index.                                                                            |
| `corners.temperature_c`  | array\<number\>   | Temperature axis, degrees Celsius. Defaults to `[27]`.                                                                                                                 |
| `corners.params`         | object            | Issue #3035. Numeric design-parameter axis: SPICE parameter name → equal-length nonempty array of finite numbers, zipped by index and expanded innermost (after temperature). Applied with `alterparam` + `reset`; each corner echoes `params` and its `corner_id` gains `/params:<pairs>`. See "Corner axes" above. |
| `exclude`                | array\<object\>   | Partial corner specs (`process`, `temperature_c`, `supply_v`, `params`) dropped from the expansion.                                                                      |
| `monte_carlo.n`          | integer           | Number of samples per expanded corner point. Positive integer, required when `monte_carlo` is present. See "Monte Carlo sampling" above.                              |
| `monte_carlo.seed`       | integer           | Base seed the sample sequence derives from. Required when `monte_carlo` is present.                                                                                    |
| `monte_carlo.vary`       | string            | `"mismatch"`, `"process"`, or `"both"` — which axis the sample sequence varies. Required when `monte_carlo` is present.                                                 |
| `monte_carlo.seed_scope` | string            | Optional; `"per_corner"` (default), `"shared"` or `"point"`. Selects what keys each sample's seeds. See "Seed scope" above.                                        |
| `monte_carlo.quantiles`  | array\<number\>   | Percentiles in `[0, 100]` reported per measurement. Defaults to `[5, 50, 95]`. See "Monte Carlo statistics" above.                                                      |
| `monte_carlo.k_sigma`    | number            | Run-wide sigma multiple `k` for the `mean ± k*stddev` limit-window check. Omit for no window check. Must be a non-negative number.                                      |
| `monte_carlo.window_scope` | string          | `"pooled"` (default) or `"per_corner"`: which `mean ± k*stddev` window gates status. `"per_corner"` fails a measurement if any `by_corner[]` window fails and adds `failing_corners` to its `monte_carlo` block; see "Pooled vs. per corner". |
| `analysis`               | object            | `kind` (e.g. `"op"`, `"dc"`, `"ac"`, `"tran"`) and `args`, the engine-syntax analysis-card arguments. One analysis per corner. `"op"` is a valid `kind`, but see `measurements[]` below — it cannot be paired with a `.meas op` card; measure an operating-point quantity with `measurements[].expr` instead. Required unless the request declares `analysis_steps` instead; declaring both is an application error (exit 1). |
| `analysis_steps[]`       | array\<object\>   | Issue #2482. Additive alternative to `analysis`: an ordered, non-empty sequence of named solves run inside each corner's one deck — each step `{name, analysis: {kind, args}, alter?, reset?, measurements?}` (`alter` values are a finite number, or `{"pwl": [t0, v0, ...]}` for a `pwl`-declared source). With it, the top-level `measurements[]` are **derived** `expr`s over `<step>.<measurement>` results. ngspice only (refused for `engine: "xyce"`); refuses `options.waveforms`/`--plot`. See "Several solves per corner (`analysis_steps[]`)" above for the full rules. |
| `measurements[]`         | array\<object\>   | `name` (stable response key) plus **exactly one** of `spice` (a verbatim `.meas` card), `expr` (issue #2533 — a single-line ngspice expression evaluated after the analysis inside `klt sim`'s own `.control` block; see "Measurements that are not `.meas` cards" above), `crossings` (issue #2868 — `{signal, level, edge?, from?, to?, max?}`, a threshold-crossing time series, `tran` only; see "Threshold-crossing time series" above), `cycle_mean` (issue #2947 — `{signal, align, level, cycles, edge?, from?}`, a time-weighted mean over whole reference cycles, `tran` only; see "Cycle-aligned mean" above), `energy` (issue #3033 — `{source, voltage, from?, to?}`, a source's net/gross/recovered energy and charge over a transient window, `tran` only; see "Source energy over a transient window" above), or `transfer` (issue #2923 — `{num, den, metric}`, an AC return-ratio metric, `ac` and local backends only; see "AC return-ratio metrics" above), plus optional `unit`, `optional` (issue #2925 — boolean, default `false`; `true` grades a value-less result `status: "absent"` instead of `error`, `spice`/`expr` on the scalar `analysis` only — see "Optional measurements" above), `limits` (`min`/`max`, either optional), `k_sigma` (per-measurement override of `monte_carlo.k_sigma`), and `plausible_range` (issue #2493 — `min`/`max`, either optional but at least one required; per-measurement plausibility bound, unscoped by `unit`; overrides `options.node_voltage_bounds` when both apply). No `limits` -> reported, never fails; no `plausible_range` (and no applicable `options.node_voltage_bounds`) -> plausibility check never runs, exactly as before this issue. Declaring both `spice` and `expr`, or neither, is an application error (exit 1). `spice`'s declared analysis type must be one ngspice's own `.MEASURE` implements (`dc`/`ac`/`tran`/`sp`) — there is no `.MEASURE OP`; a `.meas op` card is rejected up front (`SimError`), regardless of the request's own `analysis.kind`. `expr` must reduce to a single real scalar, must be one line, must have a name matching `[A-Za-z_][A-Za-z0-9_]*` unique across `measurements[]`, and is refused for `engine: "xyce"`. |
| `options.node_voltage_bounds` | object       | Issue #2493. Run-wide plausibility default (`min`/`max`, either optional but at least one required) — see "Plausibility bounds" above. **Voltage-scoped**: only auto-applies to a measurement declaring `unit: "V"` that has no own `plausible_range`. Omit for no default (today's behaviour, unchanged). |
| `options.timeout_s`      | number            | Per-corner wall-clock budget. Defaults to `120`. Exceeding it kills the process and yields an `error`-status corner.                                                    |
| `options.keep_artifacts` | boolean           | Retain per-corner logs/rawfiles on disk under `--outdir` (or its default) and reference them from the response. Defaults to `false`.                                   |
| `options.waveforms`      | boolean           | Capture the optional waveform artifact (see above). Defaults to `false`.                                                                                               |
| `options.max_workers`    | integer           | Worker-pool size for the `local-parallel` backend; ignored by `local`. Must be a positive integer. Defaults to a conservative estimate derived from the local CPU count (see "Execution backends" above). Overridable with the `--max-workers` CLI flag, and capped by the host's `KLT_SIM_MAX_WORKERS` when set (a larger value is clamped, not refused). |
| `options.wall_clock_budget_s` | number       | Overall wall-clock budget in seconds for the whole sweep. Must be a positive number. Defaults to unbounded (today's behaviour). Overridable with the `--budget-s` CLI flag. See "Wall-clock budget, orphan safety, and resume" above. |
| `options.resume`         | boolean           | Resume from a matching on-disk checkpoint under `--outdir`, skipping corners already completed by a prior interrupted run of this same request. Defaults to `false`. Overridable with the `--resume` CLI flag. Not supported with `backend: "remote"` (application error, exit 1). See "Wall-clock budget, orphan safety, and resume" above. |
| `options.fail_fast_probe` | boolean          | Two-pass fail-fast probe (issue #1694): run a bounded calibration `tran` slice on the grid's first corner before dispatching any real corner, and abort the whole grid if the measured rate implies `options.timeout_s` cannot plausibly cover the full analysis window. Defaults to `false`. Overridable with the `--fail-fast-probe` CLI flag. Only applies to `kind: "tran"` analyses on the `local`/`local-parallel` backends. See "Timeout-budget preflight" above. |
| `options.fail_on_diagnostic` | array\<string\> | Issue #2492. Diagnostic `code`s whose presence makes a corner `status: "inconclusive"` rather than `pass`/`fail` — matched on the code *before* the recovered-stepping severity downgrade, so it is how a caller says they do not trust a solve that needed gmin/source stepping to converge. Defaults to unset/`[]` (no behavior change: every count, status and statistic is exactly as before). An unrecognized code is an application error (exit 1); a valid code this run never emits is a no-op. Repeatable `--fail-on-diagnostic <code>` CLI flag overrides it. See "Grading a recovered diagnostic as inconclusive" above. |
| `options.osdi_preload`   | array\<string\>   | Issue #2513. Compiled OSDI (Verilog-A) shared libraries to load with `pre_osdi`, emitted in declared order at the top of the generated `.control` block. `$VAR`/`~` expand; relative paths resolve against the request file's directory. Each must exist (checked before any corner runs; exit 1 otherwise). Refused for `backend: "remote"`/`"batch"` unless `options.stage_model_inputs` is `true`, and always for `engine: "xyce"`. Defaults to unset (no `pre_osdi` lines — the deck is unchanged). See "OSDI (Verilog-A) model preload" above. |
| `options.stage_model_inputs` | boolean       | Issue #2668. For `backend: "remote"`/`"batch"`, upload `models.lib`, per-section corner libraries and `options.osdi_preload` binaries — with their full `.include`/file-bearing `.lib` closure — and point the worker request at the staged copies. Defaults to `false` (models resolve on the runner image, unchanged). A non-boolean is an application error (exit 1); no effect on `local`/`local-parallel`. See "Staging the request's model inputs" above. |
| `options.ngspice_binary` | string            | Issue #2423. Explicit `ngspice` binary name or path, overriding `$KLT_NGSPICE_BINARY` and the bare `ngspice` name on `$PATH`. A path containing a separator resolves relative to the request file's own directory. Only read for `engine: "ngspice"` (the default) — see "Which ngspice binary is run" above. |
| `options.save_mode`      | string            | Issue #2732. `"all"` (default; omitted means the same) keeps the generated `save all`; `"netlist"` suppresses it so the netlist's own `.save` cards decide the resident vector set, and the caller owns measurement/waveform coverage. Any other value (including `null`) is an application error (exit 1); an explicit value is refused for `engine: "xyce"`. Off-host workers must include this implementation. See "Opting out: `options.save_mode`" above. |
| `options.ngspice_options` | object | Issue #3111. Solver/convergence `.options` (`gmin`, `gminsteps`, `srcsteps`, `itl1`, `itl2`, `itl4`, `noopiter`, `method`, `reltol`, `abstol`, `vntol`, `pivtol`) rendered as one `.options` card after `.temp` in every corner deck and echoed verbatim as `environment.ngspice_options`. Values are numbers, booleans or bare-token strings; an unknown key or other value is an application error (exit 1) naming it; refused for `engine: "xyce"`. Unset: no card, no echo. See "Solver options: `options.ngspice_options`" above. |
| `options.ngspice_init`   | array\<string\>   | Issue #2520. Lines materialized as a `.spiceinit` file inside each corner's own artifact directory (`cwd=` for that corner's `ngspice` invocation), most commonly `["set ngbehavior=hsa"]` to select a compatibility mode for vendor decks written in HSPICE style. Defaults to unset — no `.spiceinit` is written (no behavior change). `engine: "ngspice"` only; accepted-and-ignored for `"xyce"`. See "ngspice's working directory, and `.spiceinit` compatibility mode" above. |
| *(CLI-only)* `--plot <dir>` | string         | No request-document equivalent (like `trajectory --plot`) — writes one waveform SVG per non-sweep signal per corner to `<dir>`, forcing `options.waveforms`/`keep_artifacts` on for this run. See "Waveform plots" above. |
| `expect_netlist_identity` | string           | Optional (issue #3074). The `environment.netlist_identity` stamp of the record this run is compared against. When present and different from the identity computed for this request, `klt sim` refuses before simulating (application error, exit 1) with a message naming both stamps. Absent or matching changes nothing. Also refused if the identity cannot be computed (see `netlist_identity_error`). |
| `netlist_source`         | string            | Optional caller-declared provenance of `netlist`: `"schematic"` (pre-layout, e.g. an S6 sizing netlist) or `"extracted"` (post-layout, from `klt extract`). Omit for unchanged behavior — the field is purely additive. An unrecognized value is an application error (exit 1). See "Post-layout verification" below. |
| `decks[]`                | array\<object\>   | Issue #2804. **Deck-list request**: declares pre-composed caller-owned ngspice decks to transport, instead of the corner matrix above. When present, `netlist`/`corners`/`analysis`/`measurements` and friends must be absent, and the report takes the deck shape (`mode: "decks"`). Per-unit fields (`id`, `deck`, `inputs`, `outputs`, `timeout_s`) and the preservation/isolation contract are documented in "Pre-composed decks" above. |

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
  "inconclusive": 0,
  "diagnostic_counts": {
    "by_code": { "singular_matrix": 1 },
    "by_severity": { "warning": 1 },
    "corners_with_diagnostics": 1
  },
  "metrics": {
    "sim__corner__count": 8,
    "sim__corner__passed_count": 8,
    "sim__corner__failed_count": 0,
    "sim__corner__errored_count": 0,
    "sim__corner__inconclusive_count": 0
  },
  "environment": {
    "engine": "ngspice",
    "engine_version": "46",
    "ngspice_binary": "/usr/bin/ngspice",
    "models_lib": { "path": null, "scope": "external" },
    "models_lib_sha256": "3ccce27a...",
    "netlist_sha256": "71d273ab...",
    "netlist_closure": [
      { "path": "tb/top.spice", "scope": "repo", "sha256": "71d273ab..." },
      { "path": "tb/shared.spice", "scope": "repo", "sha256": "9c1e07f2..." },
      { "target": "$PDK_ROOT/models.spice", "sha256": null, "unhashed_reason": "env_var" }
    ],
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
    "deck": { "name": "corner.lib", "content_hash": "sha256:<hex>" },
    "input": { "content_hash": "sha256:<hex>", "role": "netlist" }
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
      "params": { "rload": 2000 },
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
| `status`        | string          | Aggregate: `"pass"`, `"pass_partial"`, `"fail"`, `"inconclusive"`, `"error"`, or `"not_checked"`. Precedence (issue #2109's common rollup rule, extended by issues #2492/#2493): `error` > `inconclusive` > `fail` > `not_checked` > `pass_partial` > `pass`. `pass_partial` is a real, exit-`0` result — every executed check passed — that also has a nonempty `coverage.skipped` (a typo'd `limits` key beside a recognised one, a corner with no requested measurements, …); it is never reported as the unconditional `pass`. `inconclusive` is reachable only when the request declares `options.fail_on_diagnostic` and a corner emitted a listed code, or declares `options.node_voltage_bounds`/`measurements[].plausible_range` and a measured value fell outside it — see "Grading a recovered diagnostic as inconclusive" and "Plausibility bounds" above. It is the same token/exit-code pair `klt lvs` already ships (`status: "inconclusive"`, exit `4`, issue #1370): `klt sim`'s CLI exit code is `4` here, same as `error`/`not_checked`, but the token stays distinct from `"error"` — `error` means the simulator broke, `inconclusive` means it produced numbers the run will not vouch for. See "`coverage`" below. |
| `corner_count`  | integer         | Number of entries in `corners` after expansion and `exclude` — always `== len(corners)`.                        |
| `passed`/`failed`/`errored` | integer | Corner counts by status.                                                                                  |
| `inconclusive`  | integer         | Issues #2492/#2493. Corners graded `"inconclusive"` — because `options.fail_on_diagnostic` named a code the corner emitted, or because a measurement fell outside its declared plausibility bound. **One count for both reasons** (they are one status; see "One status, two reasons" above): always present, `0` for a request that declares neither, so `passed + failed + errored + inconclusive == corner_count` always holds and a consumer scanning only the original three cannot silently miscount. A nonzero value makes the aggregate `status` `"inconclusive"` (exit `4`) unless some corner also errored. The per-reason breakdown is in `diagnostic_counts.by_code` (`inconclusive` vs. `implausible_solution`). |
| `diagnostic_counts` | object      | Rollup of every `corners[].diagnostics[]` entry across the whole grid (issue #2491), counted regardless of each corner's final `status` — a `pass`ed corner with a recovered `severity: "warning"` diagnostic is still counted, as are the `inconclusive`/`implausible_solution` grading markers (issues #2492/#2493), which is how the two reasons for an `inconclusive` corner stay distinguishable. `{by_code: {<code>: N, ...}, by_severity: {<severity>: N, ...}, corners_with_diagnostics: N}`. Always present; `by_code`/`by_severity` are `{}` and `corners_with_diagnostics` is `0` for a diagnostic-free grid, never omitted. See "Failure classification" below for the `code`/`severity` vocabulary. |
| `metrics`       | object          | Declared-namespace re-keying of `corner_count`/`passed`/`failed`/`errored`/`inconclusive` (issues #1849, #2492). See below. |
| `coverage`      | object          | What this `status` was actually graded over (issue #1996) — always present, purely additive. See "`coverage`" below. |
| `environment`   | object          | Reproducibility block: engine name/version, `engine_host` (issue #3110 — `"local"` or `"worker"`; for off-host runs `ngspice_binary` is the worker's observed path or `null`, and the submitter's path is `client_ngspice_binary`; see "Which machine ran it" above), `ngspice_binary` (issue #2423 — the absolute path of the `ngspice` executable that produced this sweep's corners, as resolved from `options.ngspice_binary` / `$KLT_NGSPICE_BINARY` / `ngspice` on `$PATH`; always present-but-nullable, `null` for `engine: "xyce"` — see "Which ngspice binary is run" above), `models_lib` (the resolved model library as `{path, scope}`, issue #1274 — `{"path": null, "scope": "external"}` for the usual out-of-repo PDK, `"absent"` when nothing made one necessary — either no process axis at all, or a `corners.process` bundle whose every section named its own `lib` (issue #2522); never an absolute path) + its SHA-256, netlist SHA-256, and (when the request declares them) `osdi_preload` (issue #2513 — one `{name, path, scope, sha256}` per preloaded `.osdi`, in load order; see "OSDI (Verilog-A) model preload" above), `corner_section_libs` (issue #2522 — one `{name, path, scope, sha256}` per distinct per-section corner library a `corners.process` bundle named, in first-appearance order; see "Per-section corner libraries" above), `staged_model_inputs` (issue #2668 — only when `options.stage_model_inputs` staged model files for an off-host backend: one `{name, field, kind, sha256, source_sha256, rewritten}` per file the worker received; `models_lib_sha256_scope` (`"client"` off-host, `"local"` otherwise) qualifies `models_lib_sha256`; see "Staging the request's model inputs" above), `netlist_source`/`monte_carlo` (`{n, seed, vary}` echoed from the request, plus `seed_scope`/`quantiles`/`k_sigma` when declared, `family_mismatch` when `vary` includes `"mismatch"`, and `unseeded_sources`/`unseeded_sources_note`/`unseeded_sources_not_scanned` when the netlist declares `trnoise`/`trrandom` sources the seed does not govern (issue #2963) — see "Monte Carlo sampling" above), `transient_noise` (`{engine, sources[{name, kind}], intrinsic_device_noise, intrinsic_resistor_noise}` plus `sources_not_scanned` when part of the include closure was unscannable, only when an ngspice transient's netlist declares `trnoise`/`trrandom` sources, issue #2985 — see "Transient noise coverage" above), `budget` (when `options.wall_clock_budget_s` was declared), `orphaned: true` (only when the always-on parent-death check actually fired), and `resume` (when `options.resume` was requested — `resume.checkpoint_path` is the same `{path, scope}` shape as `netlist`, issue #1261) — see "Wall-clock budget, orphan safety, and resume" above. Also carries `timeout_preflight_warning` (string, issue #1686) when the coarse pre-grid `options.timeout_s` sanity check has something to say about a `tran` analysis's declared step/window — advisory only, never blocks the sweep, and absent for the common case — and `fail_fast_probe` (object, issue #1694) when `options.fail_fast_probe`/`--fail-fast-probe` opted in and the calibration probe ran and came back conclusive (present whether or not it aborted the grid); see "Timeout-budget preflight" above for both fields' shapes. |
| `provenance`    | object          | Shared reproducibility block (`klt_version`, `klayout_version`, `pdk`, `deck`, `input`) defined once in [`docs/json-contract.md`](../json-contract.md). `pdk` is best-effort from `models.pdk` (else `null`), and carries the additional `ambiguous_sources` key when the resolved variant was installed more than once on this host (issue #2564 — see "Model library resolution" above); `deck` pins the resolved model library (`name` = its filename, `content_hash` = `sha256:` digest) when a process axis resolved one, else `null`. `input` (issue #2039) pins the netlist under test — `{content_hash, role: "netlist"}` — always present, deliberately duplicating `environment.netlist_sha256` so `klt signoff --manifest`'s generic `provenance.input.content_hash` staleness gate and role-scoped cross-check can see a `klt sim` report the same way it already sees `klt lvs` (issue #1969 precedent); the `netlist` role means signoff never compares it against a `layout`-role hash from a `drc`/`lvs` report in the same bundle — but `klt lvs`'s pre-extracted (`layout.netlist`) request shape, `klt place-and-route`, and `klt sta`'s `verilog` request are *also* `netlist`-role (see [`docs/json-contract.md`](../json-contract.md)'s `role` table), so a bundle pairing `klt sim` with one of those **is** compared, and is refused unless both pin the same netlist file. That is the intended binding for a post-layout simulation of an extracted netlist; a schematic-level `klt sim` (this verb's usual mode) should not be bundled with a `netlist`-role `lvs`/`place-and-route`/`sta` citation of a different design stage. Complements the sim-specific `environment` block, which hashes the same library alongside the netlist. |
| `measurements`  | array\<object\> | Per-measurement rollup across all corners: `name`, `unit`, `limits`, aggregate `status` (`"pass"`/`"fail"`/`"error"`, plus `"absent"` when an `optional` measurement had no value at every corner — issue #2925 — and `"inconclusive"` when one of the contributing corners was graded so, or when this measurement's own value fell outside its plausibility bound — issues #2492/#2493, precedence `error > inconclusive > fail > pass`), and `worst_case` (the worst corner and its margin; still scanned over every corner, distrusted ones included, so an inconclusive rollup stays debuggable). Additive/optional (issue #2482): the same `step`/`derived_from` provenance fields as the per-corner entries, for an `analysis_steps[]` request. Additive/optional (issue #2493): also carries `plausible_range` when this measurement declared (or inherited from `options.node_voltage_bounds`) one. A measurement that ran under `monte_carlo` additionally carries a `monte_carlo` statistics block (`{n, errored, inconclusive, mean, stddev, min, max, quantiles, sigma_window, by_corner}`) — see "Monte Carlo statistics" above. Additive/optional (issue #1723): only present when `--plot` was used, each entry also carries `plot` — the SVG path for that measurement's own signal at its `worst_case` corner, or `null` if no rendered plot matches. See "Waveform plots" above. |
| `corners`       | array\<object\> | One entry per expanded corner, always `corner_count` entries, in the deterministic expansion order.             |
| `plots`         | array\<object\> | Additive/optional (issue #1723): only present when `--plot` was used — every SVG actually written, as `{corner_id, signal, path}`, in corner/signal order. See "Waveform plots" above. |

#### `metrics` object

Introduced by issue #1849, adopting `klayout_tools.metrics`'s declared
metric namespace registry (issue #247 — see
[`../design/metric-namespace.md`](../design/metric-namespace.md) and
[`../json-contract.md`](../json-contract.md)'s "Declared metric namespace"
section) into `klt sim`'s own top-level payload, following `klt drc`'s own
adoption (issue #1847). **Purely additive**: a re-keying of
`corner_count`/`passed`/`failed`/`errored` (plus `inconclusive`, issue
#2492) under their declared,
METRICS2.1-style hierarchical names — it never replaces or changes those
fields, which stay exactly as documented above. Always present.

| Key                            | Source field   | Description                                                                                                    |
| ------------------------------- | -------------- | ------------------------------------------------------------------------------------------------------------------ |
| `sim__corner__count`            | `corner_count` | Total number of corners in the sweep; `aggregator: "sum"`, no declared quality polarity in the registry.       |
| `sim__corner__passed_count`     | `passed`       | Number of corners that passed; `aggregator: "sum"`, `higher_is_better: true` in the registry.                  |
| `sim__corner__failed_count`     | `failed`       | Number of corners that failed; `aggregator: "sum"`, `higher_is_better: false`, `critical: true` in the registry. |
| `sim__corner__errored_count`    | `errored`      | Number of corners that errored; `aggregator: "sum"`, `higher_is_better: false`, `critical: true` in the registry. |
| `sim__corner__inconclusive_count` | `inconclusive` | Issue #2492. Number of corners graded inconclusive by `options.fail_on_diagnostic`; `aggregator: "sum"`, `higher_is_better: false`, `critical: true` in the registry — for the same reason `errored` is critical: no trustworthy result exists for those corners. Always `0` for a request that does not opt in, so an existing `klt signoff` bundle is unaffected. |

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
| `supply_v`       | object           | Supply values for this corner, keyed by voltage-source name (`{}` when the request declares no supply axis).                   |
| `temperature_c`  | number           | Temperature for this corner.                                                                                                  |
| `status`         | string           | `"pass"`, `"fail"`, `"error"`, or `"inconclusive"` (issues #2492/#2493 — only reachable when the request declares `options.fail_on_diagnostic` or a plausibility bound; see "Grading a recovered diagnostic as inconclusive" and "Plausibility bounds" above). Precedence `error > inconclusive > fail > pass`. |
| `runtime_s`      | number           | Engine wall-clock time for this corner (or time-to-timeout, on a killed run).                                                  |
| `engine_exit_code` | integer \| null | Additive (issue #2848): the simulator process's exit code (`0` or nonzero). `null` when the engine was killed by a signal, when the harness's own `timeout` ended it, when it never launched, and for corners that never ran (`budget_exceeded`, `orphaned`, `lost_shard`, …). A nonzero exit alone does not change `status`. Rides in `report.json` unchanged on `--backend batch`/`remote`. |
| `noise_representation` | string | Additive (issue #3006), `noise` corners only: `"power"` when the rawfile's integrated-noise plot is squared (`Integrated Noise - V^2 or A^2`, `voltage^2` variables — ngspice `sqrnoise` set), else `"amplitude"`. Omitted for other analyses and when no rawfile was parsed (needs `options.waveforms`/`keep_artifacts`). No `schema_version` bump. |
| `engine_killed_by_signal` | string \| null | Additive (issue #2848): the signal name (e.g. `"SIGKILL"`) when the engine process was killed by a signal the harness did not send; such a corner also carries an `engine_killed` error diagnostic. `null` otherwise, including on a harness `timeout`. |
| `measurements[]` | array\<object\>  | `name`, `value` (number, or `null` when unextractable), `unit`, `status` (`"pass"`/`"fail"`/`"error"`, plus `"absent"` for an `optional` measurement with no value — issue #2925, `value`/`margin` `null`, info diagnostic `measurement_absent` — and `"inconclusive"` when this value fell outside its declared plausibility bound — issue #2493), `margin` (`null` on that `"inconclusive"` path — `limits` never ran). An `options.fail_on_diagnostic` disqualification (issue #2492) leaves each measurement's own `status` alone — it applies to the whole corner — so a `pass` measurement inside an `inconclusive` corner is expected, and is exactly what the corner's marker diagnostic explains. Additive/optional (issue #2482, `analysis_steps[]` requests only): a step-scoped entry also carries `step` (its source step's name; its `name` is `<step>.<measurement>`), and any entry computed from other results carries `derived_from` (the `name`s of its inputs, in first-reference order). Absent for every scalar-`analysis` request. Additive/optional (issue #2868): an entry declared with `crossings` carries `value` = the crossing count and a `crossings` object `{times, edges, truncated}` (see "Threshold-crossing time series" above); absent on every other entry and when no series was extracted. Additive/optional (issue #2947): an entry declared with `cycle_mean` carries `value` = the cycle-aligned mean and a `cycle_mean` object `{t_start, t_end, cycles_used}` (see "Cycle-aligned mean" above); absent on every other entry and when no mean was computed. Additive/optional (issue #3033): an entry declared with `energy` carries `value` = the net energy (J) and an `energy` object `{net, gross, recovered, charge, from, to}` (see "Source energy over a transient window" above); absent on every other entry and when nothing was extracted. |
| `steps`          | array\<object\>  | Additive/optional (issue #2482): present only on a corner of an `analysis_steps[]` request that was dispatched to the engine — one `{name, kind, status}` per declared step, in declaration order, `status` `"ok"` when that step's analysis produced its own plot and `"error"` otherwise (it failed, or the run was killed before reaching it). Absent for scalar-`analysis` requests and for corners that never started (`budget_exceeded`, `orphaned`, …). |
| `diagnostics`    | array\<object\>  | `{ "severity": "error"\|"warning", "code": "...", "message": "..." }` — see the classification table above. `"warning"` occurs for a recovered `singular_matrix`/`nonconvergence` (does not affect `status`) and for the two grading markers, `inconclusive` and `implausible_solution` (issues #2492/#2493) — neither marker *causes* the status by itself; each records which of the two reasons graded this corner `inconclusive`. `waveform_too_large` (issue #3211) is also always a warning. Every other code is always `"error"`. A few codes carry additional code-specific fields beside these three (additive, no `schema_version` bump): `timeout_budget_unreachable`'s `reached_s`/`fraction`, `timestep_too_small`'s `time_s`/`timestep_s`/`node` (issue #2918). Empty for a clean run. |
| `artifacts`      | object           | `{"log": ..., "raw": ..., "waveform": ..., "deck": ...}`, each an absolute path or `null`. All `null` unless `options.keep_artifacts` is true; `raw`/`waveform` additionally require `options.waveforms`. `deck` is the exact per-corner ngspice deck synthesized for this corner (`.lib`/`.temp`/`alter` lines included) -- the file ngspice actually consumed, not a hash of the unexpanded source netlist (see `environment.netlist_sha256` for that). Raw log text is **never** inlined into the JSON. Additive/optional (issue #1723): also carries `plots` (array of `{signal, path}`) when `--plot` was used and this corner's waveform rendered at least one signal. |
| `monte_carlo`    | object \| null   | `null` unless this corner is a Monte Carlo sample, else `{sample_index, seed, process_seed, mismatch_seed}` (plus `seed_scope` when the request declared it) — this sample's index and its derived seed components (`seed` is the combined value written as `.options seed=` in the generated deck). See "Monte Carlo sampling" above for the seed contract and the scope of the negative control (it does not cover decks that draw mismatch from ngspice's own RNG). |

### `coverage`

The [common v1 coverage contract](../coverage-contract.md) reports actual
corner/measurement work alongside the existing `corners_simulated`,
`measurements_declared`, `measurements_with_limits` and
`unrecognized_limit_keys` fields. Those counters retain their original
meanings (`corners_simulated == corner_count`, including errored attempts);
common `checked` counts actual applied min/max bounds or deliberate
characterization observations.

An empty corner matrix (`empty_corner_matrix`), no requested measurements,
unavailable measurement values, null bounds, and unrecognized limit keys
are explicit skips. Only `min` and `max` are applied. A typo such as
`{"maximum": 1.8}` cannot count as an applied bound. Partial valid bounds
and skipped bounds remain distinguishable by identity.

A deliberate no-limit characterization counts a produced measurement as
checked and its omitted limit as inapplicable. A launched corner alone does
not count. With no failure/error, `klt sim` derives the top-level `status`
from [the common rollup rule](../coverage-contract.md#the-common-rollup-rule-version-2109)
(`coverage_rollup()`/`rollup_status()`, issue #2109) applied to this
`coverage` block: zero actual checks yields `status: "not_checked"` and
exit 4; successful checks alongside a nonempty `skipped` list — an
unrecognized `limits` key beside a recognised one on the same
measurement, an empty corner matrix, a corner with no requested
measurements, an unavailable measurement value, or a `null` bound — yields
`status: "pass_partial"` and exit 0, never the unconditional `pass`; only
known, nonempty checked work with nothing skipped earns `status: "pass"`.
Real corner errors and failed bounds (including Monte Carlo sigma windows)
retain precedence over coverage and their existing exit codes. `klt signoff`
refuses zero/unknown/malformed common coverage and never reads a
`pass_partial` run as fully qualifying evidence (`partial_coverage`); see
[`coverage-contract.md`](../coverage-contract.md)'s "Signoff qualification"
section.

### Semantics and guarantees

Carried over from the spike's proposed contract (see the spike document for
the full reasoning):

- **`fail`, `error`, and `inconclusive` are always different.** `error`
  means no trustworthy number exists (nonconvergence, singular matrix,
  timeout, netlist error, an unextractable measurement); `fail` means the
  simulator produced a trustworthy number and the design missed a limit;
  `inconclusive` (issues #2492/#2493) means the simulator produced a number
  the run will not vouch for — either its solve emitted a diagnostic code the
  request listed in `options.fail_on_diagnostic`, or it fell outside a
  declared plausibility bound — and so is not known to be trustworthy enough
  to say either. See "Grading a recovered diagnostic as inconclusive" and
  "Plausibility bounds" above, and "One status, two reasons" for why both
  reasons share the one token.
- **Aggregate precedence: `error` > `inconclusive` > `fail` > `pass`**, at
  both the corner level (any `error`-severity diagnostic forces `error`,
  regardless of measurement outcomes) and the response level (any errored
  corner makes the whole run `error`). `inconclusive` is reachable only when
  the request declares `options.fail_on_diagnostic` and a corner emitted a
  listed code, or declares
  `options.node_voltage_bounds`/`measurements[].plausible_range` and some
  measured value fell outside it; it is never reached otherwise, so this
  extension does not change the precedence for any pre-existing request. A
  declared Monte Carlo sigma window that falls outside the limits is the one
  way the response can be `fail` with every *corner* passing — it is a
  statement about the sampled population, not about any single run (see
  "Monte Carlo statistics" above). It never overrides `error` or
  `inconclusive`.
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
  a never-dispatched `budget_exceeded`/`orphaned` corner, …) and
  `inconclusive` ones (issue #2492 — a number the caller declared
  untrustworthy) did not (issue
  [#1686](https://github.com/2AMLogic/klayout-tools/issues/1686)). A
  response where every corner errored — the exact shape an unmeetable
  `options.timeout_s` budget produces — is a record with **no** measurement
  data in it; a downstream reader (human or agent) should treat it as "the
  sweep produced no evidence", never mistake its `corner_count` for a count
  of usable rows.
- **Reproducibility is in-band** — `environment` hashes the netlist and
  resolved model library so a stored result can be checked against the
  inputs that produced it; a `monte_carlo` request additionally makes the
  derived per-sample seed integers themselves reproducible from
  `monte_carlo.seed` alone — the draws ngspice makes from them are only
  reproducible on the same engine build (see "Seed contract." under "Monte
  Carlo sampling" above).

## Output-directory ownership

**Output-directory ownership (issue #2770).** A `klt sim` run that writes
persistent artifacts -- `options.keep_artifacts`, `--resume` (the
checkpoint), `--plot` (the plot directory, also owned), or the
`remote`/`batch` backends that collect results locally -- owns `--outdir`
(default `.klt/sim/` next to the request) for the whole run. A second
cooperating run on the same directory (any relative or symlink alias of it)
-- or on an ancestor or descendant of it -- fails immediately with error
code `outdir_locked`: exit `1`, empty stdout, the standard error envelope on
stderr, and nothing under the directory modified. A run that uses only
temporary directories writes nothing there and takes no lock;
sharded/parallel workers inside one run share the run's ownership. A killed
or crashed run releases ownership automatically; a leftover owner record
never blocks the next run. `outdir_lock_unavailable` (same exit/streams)
means locking itself is not possible here. Full policy, library exceptions
and platform scope: [json-contract.md](../json-contract.md)
"Output-directory ownership".

## Exit codes

| Code | Meaning                                                                    |
| ---- | ---------------------------------------------------------------------------- |
| `0`  | Checked at least one measurement/bound and no corner or rollup failed. Aggregate `status` is `"pass"` (nothing requested was skipped) or `"pass_partial"` (issue #2109 — every executed check passed, but `coverage.skipped` is nonempty). For a **deck-list request** ("Pre-composed decks" above): every unit ran to completion with engine exit 0.                                                         |
| `1`  | Failed to run at all — bad/malformed request, unresolvable netlist or model library, unsupported engine, unknown backend. |
| `2`  | Usage error (missing argument, bad `--format` value) — from argparse.        |
| `3`  | Ran successfully; at least one measurement failed a limit (aggregate `status: "fail"`), every corner produced a usable result. Includes a declared Monte Carlo `mean ± k*sigma` window falling outside the limits, even when every individual sample passed. Cannot arise for a deck-list request (no graded measurements). |
| `4`  | No actual measurement/bound was checked (`status: "not_checked"`), at least one corner errored (aggregate `status: "error"`), or at least one corner was graded `"inconclusive"` (aggregate `status: "inconclusive"` — `options.fail_on_diagnostic` named a code the solve emitted, issue #2492, or a measured value fell outside a declared `options.node_voltage_bounds`/`measurements[].plausible_range`, issue #2493; see "Grading a recovered diagnostic as inconclusive" and "Plausibility bounds" above) — the sweep is incomplete or untrustworthy. Also covers a corner that never ran because `options.wall_clock_budget_s` was exceeded, the launching process exited, or `options.fail_fast_probe` aborted the grid (`budget_exceeded`/`orphaned`/`timeout_budget_unreachable` diagnostics) — those corners are `"error"` too, not silently omitted; see "Wall-clock budget, orphan safety, and resume" and "Timeout-budget preflight" above. For a **deck-list request**: at least one unit errored or timed out — per-unit exit codes, streams, and missing-output lists say which and why. |

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

**Gate on `status`, not the exit code.** These codes are additive — a
future release may add a new one above `4` — and the exit code is only a
shortcut derived from the payload's own `status` field, which is
authoritative. See [`docs/json-contract.md`](../json-contract.md#exit-codes)'s
"Exit codes" section.

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
construction (see
[`../design/numeric-fixture-comparison-convention.md`](../design/numeric-fixture-comparison-convention.md)
for the repo-wide convention this follows: declare which regions of a
numeric fixture are byte-exact vs. tolerance-compared, never a blanket
comparison style for the whole artifact) — but running

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

### `environment.netlist_closure` (issue #2799)

`environment.netlist_sha256` digests only the top-level `netlist`. Additively,
`environment.netlist_closure` lists the resolved `.include`/`.inc` closure --
the netlist first, then each include in resolution order -- using the same
resolver the `remote`/`batch` backends stage with (issue #2485), so local and
off-host reports of one request describe the same set. A resolved file is
`{"path", "scope", "sha256"}` (`path`/`scope` as in `models_lib`; `sha256` is
of the file's on-disk bytes). A directive left to the executing host is
`{"target", "sha256": null, "unhashed_reason"}` with reason `env_var` or
`pdk_root`. If the closure cannot be resolved (e.g. a missing include),
`environment.netlist_closure_error` carries the message instead and the report
is otherwise unchanged.

### `environment.netlist_identity` (issue #3074)

`environment.netlist_sha256` and `netlist_closure` hash raw bytes, so a comment
edit or a moved include changes them. `environment.netlist_identity` is the
comparable stamp: `sha256:<hex>` over the netlist and its resolved
`.include`/`.inc` closure (resolution order, as in `netlist_closure`) after
dropping `*` comment lines and trailing `;`/`$` comments and blank lines,
folding `+` continuations, collapsing whitespace, folding case, and omitting
the *target text* of `.include`/`.inc` directives (content, not location).
Line order is **not** normalised (`.param`/`.model` order can matter), so a
reorder is a mismatch: a false mismatch is safe, a false match is not.

The identity is `null`, with `environment.netlist_identity_error`, when the
closure cannot be fully read (an unresolvable include, or an env-var/PDK-rooted
include left to the executing host). It is computed client-side, so `local`,
`remote` and `batch` report the same value for one request. `klt sim` stays
stateless: to guard a comparison, copy the earlier report's stamp into the new
request's `expect_netlist_identity`.
