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
| `remote`           | Reserved for a future phase (Epic #253) — not implemented; requesting it is an error.                                |

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
| `backend`                | string            | Execution backend for the corner matrix. Defaults to `"local"` (runs corners sequentially in-process); `"local-parallel"` runs the same matrix across a bounded local worker pool. `"remote"` is reserved but unimplemented. See "Execution backends" above. Overridable with the `--backend` CLI flag. |
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
    "netlist_sha256": "71d273ab..."
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
| `environment`   | object          | Reproducibility block: engine name/version, resolved model-library path + SHA-256, netlist SHA-256.             |
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
