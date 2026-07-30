# Spike: SPICE PVT corner runner

**Status:** spike / proposal. Nothing here is scheduled, and nothing here
authorises implementation. Per
[docs/ARCHITECTURE.md](../ARCHITECTURE.md) → "How capabilities arrive," a
major capability arrives by spiking a design epic first — candidate-engine
survey, proposed JSON contract, wrap/build decision — and this document is
that spike for SPICE simulation. A follow-up epic would carry the build.

**Demand signal:** private block repos are each standing up their own
xschem + ngspice PVT harness (sweep process corner × supply × temperature,
emit machine-readable pass/fail), and the propagation mechanism is
"copy the harness from the first repo that got it working." Repo-to-repo
copy-paste of a harness is the friction-log signal that a capability is
tool-owned rather than per-repo scaffolding
([ROADMAP.md](../../ROADMAP.md) → "How progress is driven").

**What is being copy-pasted is not a simulator.** It is corner-matrix
expansion, per-run invocation and timeout handling, measurement extraction
from text logs, and pass/fail collation. That observation drives both the
contract below and the wrap/build call in the last section.

## 1. Candidate-engine survey

### ngspice

| Property | Finding |
| -------- | ------- |
| Upstream | [ngspice](https://ngspice.sourceforge.io/) — active, descended from Berkeley SPICE3f5. Regular releases. |
| Licence | Core is BSD-3-Clause (Berkeley lineage); contributed subsystems carry their own permissive terms (XSPICE from Georgia Tech, CIDER from UC Berkeley). Builds configured with the KLU solver pull SuiteSparse (LGPL). **Compatible with our MIT surface as an invoked binary**; if a build is ever redistributed, the solver question needs a real answer. |
| Headless batch | First-class. `ngspice -b netlist.cir` runs batch with no GUI/Tcl; `-o` sets the log, `-r` writes a rawfile. A `.control … .endc` block scripts the run (`alter`, `altermod`, `dc`, `tran`, `meas`, `let`, `print`, `wrdata`, `foreach`, `quit`). Ships as a single OS package on Linux and macOS — trivially CI-installable. |
| Also embeddable | `libngspice` exposes a C API (`ngSpice_Init`, `ngSpice_Command`, `ngGet_Vec_Info`), which is what PySpice wraps. See "invocation strategy" below for why the spike does **not** recommend this path for v1. |
| Native PVT sweeping | **Partial, and asymmetric across the three axes** — the single most important finding: <ul><li>**Process corner** — selected by `.lib <path> <section>` (sky130A: `libs.tech/ngspice/sky130.lib.spice` with sections `tt`, `ss`, `ff`, `sf`, `fs`, plus the `*_mm` mismatch variants). Section selection is resolved at netlist **parse** time, so changing the process corner means re-parsing — in practice, a fresh process.</li><li>**Supply** — cheap. `alter vdd=1.62` inside `.control` mutates a source and re-runs without re-parsing.</li><li>**Temperature** — settable per run (`.temp`, `.options temp=`, or `set temp=` in `.control`), but it invalidates temperature-dependent model quantities, so looping it in-process is the fragile case.</li></ul>ngspice has **no `.step` card** (that is an LTspice/HSPICE-ism). Multi-point sweeps are hand-rolled with `.control` loops. There is therefore no engine-native concept of "corner matrix" at all — which is precisely the gap the copy-pasted harnesses are filling. |
| Structured results | **No JSON, at any layer.** Options, worst to best: (a) scrape the human log — what the copy-pasted harnesses do, and the reason they are brittle; (b) the rawfile (`-r`, with `set filetype=ascii`) — a documented header/`Values:` format, parseable but bulky and waveform-shaped rather than scalar-shaped; (c) `.meas` statements to reduce waveforms to scalars in-engine, then `print`/`wrdata` those scalars to a file — small, deterministic, and the right primitive for pass/fail; (d) `libngspice` vectors read directly from memory. **(c) is the recommended extraction path**, with (b) retained as an optional artifact when a caller wants waveforms. |
| Failure signalling | Weak, and a wrapper hazard. Exit status is not a trustworthy pass/fail or even success signal; nonconvergence surfaces as log text (`doAnalyses: iteration limit reached`, singular-matrix errors) while the process may still exit 0, and a hard convergence failure can hang rather than exit. Any wrapper **must** impose a per-run timeout and classify diagnostics from the log, not from the exit code. |

### Xyce (contrast candidate)

Surveyed per [docs/ARCHITECTURE.md](../ARCHITECTURE.md) → "Mining the
outside world," which names Xyce explicitly.

| Property | Finding |
| -------- | ------- |
| Upstream | [Xyce](https://xyce.sandia.gov/) — Sandia National Laboratories, open-sourced at 6.0. |
| Licence | GPL-3.0. Fine to invoke as a separate process; it forecloses in-process embedding inside an MIT-licensed library, which permanently constrains the implementation options. |
| Headless batch | First-class and designed for it — no GUI exists. Built for MPI/HPC batch. |
| Native PVT sweeping | **Better than ngspice.** `.STEP` sweeps parameters natively, including nested steps and `TEMP`, and `.SAMPLING` / `.EMBEDDEDSAMPLING` cover Monte Carlo and UQ. `.LIB` sections work, so process corners still need separate invocations — but supply × temperature could be one process, natively. |
| Structured results | Better than ngspice. `.PRINT … FORMAT=CSV` (and `FORMAT=RAW`) give column output without log scraping, and `.MEASURE` results are emitted to their own files. Still not JSON. |
| Practical blockers | Two, both decisive for a first implementation: (1) **open-PDK fit** — sky130A ships its device models as an *ngspice* deck (`libs.tech/ngspice/`), and the surrounding open flow (xschem, magic, netgen, OpenLane) is calibrated against ngspice; running the same corners under Xyce is a model-translation project, not a flag. Our rule is open PDKs only, so the PDK's native simulator wins by default. (2) **installability** — Xyce is a from-source Trilinos build in most environments versus a one-line package install for ngspice, which matters for "every command must be runnable in CI." |

Also considered and set aside: **Qucs-S** (a GUI front-end that drives
ngspice/Xyce — not an engine, and GUI-first, so out by the headless rule);
**Spectre / HSPICE / AFS** (proprietary, and inseparable from
proprietary-PDK workflows — out by the open-PDK rule); **OpenVAF** (a Rust
Verilog-A compiler, complementary to a simulator rather than a
replacement — relevant to a future device-model story, not to corner
running).

### Recommendation

**ngspice for v1, behind a contract that does not name it.** Xyce is the
technically stronger sweeper — native `.STEP`, native CSV, native UQ — and
the honest reason it loses is ecosystem, not capability: sky130's models,
the xschem flow the block repos already use, and the friction being
reported are all ngspice-shaped, and translating models to reach a better
sweep primitive is a larger project than building the sweep primitive
ourselves.

Because the contract is the API and the engine is an implementation detail,
this is a reversible choice. The request shape below carries an explicit
`engine` selector, and Xyce is the intended second implementation — the
right first test of whether the contract really is engine-agnostic. Where
the two engines disagree in output, the contract's field semantics win.

### Invocation strategy (one process per corner)

Recommended: a plain subprocess per corner (`ngspice -b`, per-run timeout,
kill on expiry), **not** in-process `libngspice`/PySpice, for four reasons:

1. SPICE can hang on nonconvergence. A hung engine inside the agent's own
   process is unrecoverable; a hung child process is a `kill` and an
   `error`-status corner.
2. Process-corner `.lib` selection needs a re-parse anyway (above), so
   per-corner processes cost little that a shared process would save.
3. Process fan-out is the whole parallelism story — N corners across M
   cores with no shared engine state.
4. It sidesteps the solver-licence question and `libngspice`'s
   one-circuit-at-a-time state model entirely.

The corner matrix is expanded by us, one invocation per point. That is a
deliberate rejection of "make the engine do the sweep": the engine that
sweeps best is not the engine the PDK supports, and the expansion logic is
the reusable part.

## 2. Proposed JSON contract

Documented in the field-table style already established for `klt layers`
(see [docs/cli/layers.md](../cli/layers.md) → "JSON schema (the
contract)"). Same rules apply: **JSON is the API**, text output is a
courtesy, renaming/removing/retyping a field is a breaking change, new
fields may be added, consumers ignore unknown fields. Units are carried in
field names (`_v`, `_c`, `_s`) the way `layers` carries `dbu_um`.

This is a **proposed** shape for review, not a shipped contract.

### Request

```json
{
  "schema": "klt.sim.corners/1",
  "netlist": "blocks/bandgap/bandgap.spice",
  "engine": "ngspice",
  "models": {
    "lib": "$PDK_ROOT/sky130A/libs.tech/ngspice/sky130.lib.spice"
  },
  "corners": {
    "process": ["tt", "ss", "ff", "sf", "fs"],
    "supply_v": { "vdd": [1.62, 1.80, 1.98] },
    "temperature_c": [-40, 27, 125]
  },
  "exclude": [{ "process": "sf", "temperature_c": -40 }],
  "analysis": { "kind": "tran", "args": "1n 200u" },
  "measurements": [
    {
      "name": "vref",
      "spice": ".meas tran vref AVG v(vref) FROM=150u TO=200u",
      "unit": "V",
      "limits": { "min": 1.19, "max": 1.21 }
    },
    {
      "name": "iq",
      "spice": ".meas tran iq AVG i(vvdd) FROM=150u TO=200u",
      "unit": "A",
      "limits": { "max": 5e-6 }
    }
  ],
  "options": { "timeout_s": 120, "max_parallel": 8, "keep_artifacts": true }
}
```

| Field                    | Type              | Description                                                                                                                                                              |
| ------------------------ | ----------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `schema`                 | string            | Contract identifier and major version. Bumped on any breaking change.                                                                                                    |
| `netlist`                | string            | Path to the netlist under test, as provided. A *reference*, not inline text — so an xschem export and a Phase-4 extracted netlist are interchangeable inputs.             |
| `engine`                 | string            | Engine selector (`"ngspice"` for v1). Present from day one so engine choice is data, not a code path.                                                                     |
| `models.lib`             | string            | Model library to bind process-corner sections from. Env vars in the path are expanded (`$PDK_ROOT`), and the resolved path is echoed in the response.                     |
| `corners.process`        | array\<string\>   | Process-corner axis. Values are **opaque section names** passed through to the model library — so sky130 mismatch sections (`tt_mm`) or a vendor's naming need no schema change. |
| `corners.supply_v`       | object            | Supply axis, keyed by source name (`vdd`, `vdda`, …), each mapping to an array of volts. Multiple keys sweep **together** by index (rails move as a set), not as a cross product. |
| `corners.temperature_c`  | array\<number\>   | Temperature axis, degrees Celsius.                                                                                                                                       |
| `exclude`                | array\<object\>   | Optional. Partial corner specifications to drop from the expansion, for sparse matrices (skip the physically uninteresting extremes without abandoning axis declaration). |
| `analysis`               | object            | The analysis to run at every corner: `kind` (`op`, `dc`, `ac`, `tran`) plus engine-syntax `args`. One analysis per request in v1.                                          |
| `measurements[]`         | array\<object\>   | What is measured and what "pass" means. `name` is the stable key used in the response; `spice` is the engine-syntax measurement statement; `unit` is informational; `limits` (`min` and/or `max`, either optional) define pass/fail. A measurement with no `limits` is **reported but never fails** — the documented way to collect a value you are still characterising. |
| `options.timeout_s`      | integer           | Per-corner wall-clock budget. Exceeding it yields an `error`-status corner, never a silent hang.                                                                          |
| `options.max_parallel`   | integer           | Maximum concurrent engine processes.                                                                                                                                     |
| `options.keep_artifacts` | boolean           | Retain per-corner logs/rawfiles on disk and reference them from the response.                                                                                             |

### Response

```json
{
  "schema": "klt.sim.corners/1",
  "netlist": "blocks/bandgap/bandgap.spice",
  "status": "fail",
  "corner_count": 42,
  "passed": 40,
  "failed": 2,
  "errored": 0,
  "environment": {
    "engine": "ngspice",
    "engine_version": "43",
    "models_lib": "/pdk/sky130A/libs.tech/ngspice/sky130.lib.spice",
    "models_lib_sha256": "9f2c…",
    "netlist_sha256": "1ab7…"
  },
  "measurements": [
    {
      "name": "vref",
      "unit": "V",
      "limits": { "min": 1.19, "max": 1.21 },
      "status": "fail",
      "worst_case": { "corner_id": "ss/1.620V/125C", "value": 1.2134, "margin": -0.0034 }
    },
    {
      "name": "iq",
      "unit": "A",
      "limits": { "max": 5e-6 },
      "status": "pass",
      "worst_case": { "corner_id": "ff/1.980V/125C", "value": 4.4e-6, "margin": 6e-7 }
    }
  ],
  "corners": [
    {
      "corner_id": "ss/1.620V/125C",
      "process": "ss",
      "supply_v": { "vdd": 1.62 },
      "temperature_c": 125,
      "status": "fail",
      "runtime_s": 6.4,
      "measurements": [
        { "name": "vref", "value": 1.2134, "unit": "V", "status": "fail", "margin": -0.0034 },
        { "name": "iq", "value": 3.9e-6, "unit": "A", "status": "pass", "margin": 1.1e-6 }
      ],
      "diagnostics": [],
      "artifacts": { "log": ".klt/sim/ss_1p620_125/ngspice.log", "raw": null }
    }
  ]
}
```

#### Top-level fields

| Field           | Type            | Description                                                                                                   |
| --------------- | --------------- | ------------------------------------------------------------------------------------------------------------- |
| `schema`        | string          | Echo of the request's contract identifier.                                                                    |
| `netlist`       | string          | Echo of the input path exactly as provided (matches `layers`' `file` convention).                              |
| `status`        | string          | Aggregate: `pass`, `fail`, or `error`. See precedence below.                                                   |
| `corner_count`  | integer         | Number of entries in `corners` after expansion and `exclude`.                                                 |
| `passed`        | integer         | Corners with status `pass`.                                                                                   |
| `failed`        | integer         | Corners with status `fail` (a measurement violated a limit).                                                  |
| `errored`       | integer         | Corners with status `error` (no usable result was produced).                                                  |
| `environment`   | object          | Reproducibility block: engine name and version, resolved model-library path and hash, netlist hash.            |
| `measurements`  | array\<object\> | Per-measurement rollup across all corners, including the worst case and which corner produced it.              |
| `corners`       | array\<object\> | One entry per expanded corner (see below).                                                                     |

#### `corners[]` entries

| Field            | Type             | Description                                                                                                                   |
| ---------------- | ---------------- | ----------------------------------------------------------------------------------------------------------------------------- |
| `corner_id`      | string           | Stable, human-readable identity: `<process>/<supply>V/<temp>C` (e.g. `ss/1.620V/125C`). Supply formatted to 3 decimals so ids sort and diff stably. |
| `process`        | string           | Process-corner section used.                                                                                                  |
| `supply_v`       | object           | Supply values for this corner, keyed by source name.                                                                          |
| `temperature_c`  | number           | Temperature for this corner.                                                                                                  |
| `status`         | string           | `pass`, `fail`, or `error`.                                                                                                   |
| `runtime_s`      | number           | Engine wall-clock time for this corner.                                                                                       |
| `measurements[]` | array\<object\>  | `name`, `value` (number, or `null` when unextractable), `unit`, `status`, `margin`.                                            |
| `diagnostics`    | array\<object\>  | Structured engine complaints: `{ "severity": "error"\|"warning", "code": "nonconvergence"\|"timeout"\|"netlist"\|"unknown", "message": "…" }`. Empty for a clean run. |
| `artifacts`      | object           | Paths to retained `log` / `raw` files, or `null` per key when not retained. Raw logs are **never** inlined into the JSON.       |

#### Semantics and guarantees

- **`fail` and `error` are different, always.** `fail` means the simulator
  produced a trustworthy number and the design missed a limit. `error`
  means no trustworthy number exists — nonconvergence, timeout, netlist
  error, unextractable measurement. Conflating them is the specific defect
  in log-scraping harnesses: a nonconverged corner reads as "no violation
  found" and passes silently. An agent must be able to tell "the circuit is
  wrong" from "the run is broken," because the corrective action differs.
- **Aggregate precedence: `error` > `fail` > `pass`.** Any errored corner
  makes the whole run `error`, even if every completed corner passed — an
  incomplete sweep is not a passing sweep.
- **Deterministic expansion and ordering.** `corners` is the full cross
  product of the declared axes minus `exclude`, emitted odometer-style in
  axis-declaration order (process outermost, temperature innermost), so
  output is byte-stable across runs and platforms regardless of the order
  results actually complete in. Same guarantee `layers` makes about its
  sort order.
- **`margin` sign convention.** Signed distance to the nearest violated or
  nearest binding limit, in the measurement's units: **positive is
  headroom, negative is violation**. `null` when `value` is `null`. This is
  what makes "how close are we?" answerable without re-deriving limits
  client-side, and what makes worst-case ranking well defined.
- **A missing measurement is an error, not a pass.** If a `.meas` yields no
  value, that corner is `error` with a diagnostic, and `value` is `null`.
- **Every corner is reported.** Errored corners appear in `corners` with
  their diagnostics; the array length always equals `corner_count`.
- **Reproducibility is in-band.** `environment` hashes the netlist and
  model library so a stored result can be checked against the inputs that
  produced it — a corner report that cannot be re-derived is not evidence.
- **Room to grow without breaking.** Monte Carlo / mismatch is out of scope
  for v1, but `corners.process` taking opaque section names means sky130's
  `*_mm` sections fit already, and a future `samples` axis is an additive
  field.

#### Proposed exit codes

| Exit code | Meaning                                                                        |
| --------- | ------------------------------------------------------------------------------ |
| `0`       | All corners passed.                                                            |
| `1`       | At least one corner failed a limit; every corner produced a usable result.     |
| `2`       | Usage error (bad arguments, malformed request) — from argparse.                |
| `3`       | At least one corner errored — the sweep is incomplete or untrustworthy.        |

**Open question for the epic, flagged deliberately:** `klt layers` uses `1`
for input errors and `2` for usage. A pass/violation/broken trichotomy does
not fit that two-code shape, and `klt drc` (Phase 2) will need exactly the
same trichotomy. The repo-wide convention should be settled once, when
Phase 2 lands, rather than invented twice; the table above is this spike's
proposal, not a fait accompli.

## 3. Wrap or build?

[docs/ARCHITECTURE.md](../ARCHITECTURE.md) → "Rewrite rule" permits a
rewrite only when **all three** hold, and it names SPICE numerics and
device models as a poor target (BSIM4/PSP are a moving target maintained by
the Compact Model Coalition; convergence heuristics encode decades of edge
cases). Engaging with the test rather than deferring to the conclusion:

1. **Bottleneck or ceiling — fails.** The measured friction is not the
   solver. It is corner-matrix expansion, invocation and timeout handling,
   log scraping, and result collation — all *above* the engine. Nobody is
   copy-pasting a harness because ngspice integrates too slowly. The
   plausible future bottleneck is sweep throughput (N corners × M
   measurements), and the answer to that is process fan-out across cores,
   not a faster inner solver.
2. **Oracle exists — holds.** This is the one part that passes: a
   reimplementation could be diffed corner-by-corner against ngspice
   through the very contract proposed above. Worth noting because it means
   a rewrite *would* be safely testable later, if the other two ever flip.
3. **Unlock — fails today.** Nothing this contract requires is structurally
   impossible through a subprocess wrapper. The honest counter-case is
   gradient-based sizing: a differentiable, in-loop simulator would unlock
   something a wrapper structurally cannot, and this org has adjacent prior
   art (geode-fem is differentiable end-to-end). But that is a different
   capability than PVT corner running, and even then the leverage move is
   the OpenVAF-style one — take the modern implementation the field already
   ships rather than reimplement BSIM4.

One of three. **Recommendation: wrap ngspice.**

**But "wrap" is the wrong word for the whole answer, and this is the
substantive point of the spike.** The decision is not binary, because two
different things are in play:

- **Wrap the numerics.** Solver, device models, convergence heuristics —
  ngspice, unmodified, invoked as a subprocess. Poor rewrite target, per
  the architecture doc, and nothing here argues otherwise.
- **Build the orchestration.** Corner-matrix expansion, deterministic
  corner ids, per-run timeout and kill, `fail`/`error` classification,
  measurement extraction, worst-case rollup, JSON emission. There is
  **nothing to wrap here** — no engine ships this, which is exactly why
  every block repo has hand-rolled it. This layer is ours, first-class,
  written from scratch.

Reading this as "wrap ngspice" alone would reproduce the friction: a thin
shell around `ngspice -b` that hands back a log leaves every consumer to
rebuild the copy-pasted part. The engine is a dependency behind the
contract; the corner runner *is* the contract.

## Out of scope for this spike

No dependency was added to `pyproject.toml`, no `klt` subcommand was added,
no simulator-invoking code was written, and no MCP surface was touched.
Those remain candidate follow-up epics gated on this spike's findings.

## Open questions for a follow-up epic

- Exit-code convention shared with `klt drc` (above).
- Where corner-matrix definitions live — inline CLI flags, a request JSON
  file, or a per-block committed spec — and how a spec is versioned
  alongside the netlist it tests.
- Whether measurements stay engine-syntax `.meas` strings (portable to
  Xyce's `.MEASURE`? partially) or get an engine-neutral abstraction. v1
  chooses pass-through for honesty; a second engine will test that choice.
- Netlist provenance: how a Phase-4 extracted netlist and an xschem
  schematic netlist are distinguished in `environment`, so a corner report
  states which side of the loop it verified.
- Caching: corner results are pure functions of
  (netlist, models, corner, analysis) hashes, so re-running a sweep after a
  one-corner fix need not re-simulate the matrix.
