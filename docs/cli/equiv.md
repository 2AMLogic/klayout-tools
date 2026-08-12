# `klt equiv`

Prove or refute **combinational** equivalence between two RTL/gate-level
netlists (`gold` and `gate`) via Yosys's built-in miter/SAT flow — Phase 0
of the formal-equivalence epic
[#707](https://github.com/2AMLogic/klayout-tools/issues/707), the
correctness loop-closer
[#704](https://github.com/2AMLogic/klayout-tools/issues/704) (RTL
synthesis) and [#700](https://github.com/2AMLogic/klayout-tools/issues/700)
(place-and-route) both name as their own verification step.
[`klt synthesize --verify-equivalence`](synthesize.md#equivalence-gate)
wires this command in directly as `klt synthesize`'s own acceptance gate
(#704 Phase 1) — see that flag's docs for the wired, one-command version of
the "synthesize, then check" flow this document describes standalone.

```
klt equiv <request> [--timeout-s <seconds>] [--format text|json]
```

Like `klt lvs`/`klt sim`/`klt synthesize`, `klt equiv` takes a **request
document**, not positional netlist file args — two RTL/gate-level sides
plus optional I/O mapping and a timeout is richer than a flag line carries
cleanly.

- `<request>` — a request document (see "Request" below), in any of three
  forms, mirroring `klt lvs`'s path-or-stdin-or-inline convention:
  - a **path** to a request JSON file, e.g. `klt equiv request.json`.
  - `-` to read the request JSON document from **stdin**.
  - an **inline JSON object** string, e.g.
    `klt equiv '{"gold": {...}, "gate": {...}}'`. An existing file always
    wins first.
  - **Relative paths inside the request** (`gold.sources`, `gate.liberty`,
    etc.) resolve against the **request file's own directory** for the
    path form, but against the **current working directory** for the
    stdin and inline-JSON forms.
- `--timeout-s` — overall wall-clock timeout in seconds for the whole
  Yosys proof (default: `60`, or the request's own `timeout_s` field);
  overrides the request field when given. A run that does not finish
  within this budget is reported `"inconclusive"` — **never**
  `"equivalent"`. See "Timeout and the inconclusive verdict" below.
- `--format` — `text` (default) or `json`.

## Scope: combinational only (Phase 0)

This is deliberately narrow. A design containing flip-flops, latches, or
memories on either side is rejected up front with a clear scope error
(exit `1`) — not silently checked per-cycle and reported with a
misleadingly confident verdict. Sequential equivalence (temporal
induction / bounded model checking, orchestrated via SymbiYosys) is a
later phase of #707, once it can be exercised end to end in this repo's
own CI (see "Engine" below).

## Engine

`request.engine` is a data field, not a code path (matching every other
digital-flow verb's own precedent — `synthesize.py`,
`functional_verification.py`) — only `"yosys"` is implemented today. An
unsupported value is an application error (exit `1`).

### `"yosys"` (default, only value implemented)

Orchestrates Yosys's own built-in equivalence-checking primitives directly
— the same `miter -equiv` / `sat -prove-asserts` recipe the Yosys manual's
own equivalence-checking chapter documents (built-in MiniSat SAT solver,
no external SAT-solver or `sby`/SymbiYosys dependency). Two subprocesses
run:

1. `yosys -s <script>` builds a miter circuit (`gold != gate` for some
   legal input assignment) and asks Yosys's SAT backend to prove or
   refute it.
2. **On a refutation, the counterexample is independently confirmed by
   simulation before it is ever reported.** `iverilog`/`vvp` (already a
   first-class dependency via `klt functional-verification`) compiles the
   two flattened netlists Yosys itself just proved diverge, drives the
   *exact* concrete input vector the solver reported, and confirms the
   divergence actually reproduces — `counterexample.confirmed_by_simulation`
   in the response. This is the epic's own "a counterexample is
   executable, never solver-output trusted uncritically" discipline.
   `klt sim`'s own simulation-invocation path (`sim.py`) is SPICE/analog
   (`ngspice -b` on a resistor/transistor-level netlist) and is not the
   right tool for a gate-level RTL vector — `iverilog`/`vvp` is the
   digital equivalent, reused here.

**Why plain Yosys, not SymbiYosys.** This repo's CI
(`.github/workflows/ci.yml`) installs a real `yosys` binary but no
`sby`/SymbiYosys — and SymbiYosys's main value (orchestrating multi-step
*sequential* proofs) is exactly what this MVP's combinational-only scope
does not need. Rather than stub an orchestration interface no build of
this repo can exercise, this engine runs the same recipe SymbiYosys's own
`mode equiv` expands to for a single-cycle proof. A `sby`-backed
`"engine": "symbiyosys"` for sequential designs is left for a later phase
of #707.

## Request

```json
{
  "gold": {
    "sources": ["adder4.v"],
    "top": "adder4"
  },
  "gate": {
    "sources": [".klt/synthesize/adder4_synth.v"],
    "top": "adder4",
    "liberty": "/path/to/sky130_fd_sc_hd__tt_025C_1v80.lib"
  },
  "port_map": null,
  "engine": "yosys",
  "timeout_s": 60
}
```

| Field | Type | Description |
| --- | --- | --- |
| `gold` / `gate` | object | The two sides to compare. Required. |
| `gold.sources` / `gate.sources` | array\<string\> | RTL/gate-level Verilog source file paths (`read_verilog` inputs), resolved relative to the request's own directory. Required, non-empty. |
| `gold.top` / `gate.top` | string | That side's top module name. The two sides may legally use the *same* top module name (e.g. both `"top"`) — each is elaborated in its own namespace before comparison. Required. |
| `gold.liberty` / `gate.liberty` | string \| omitted | A standard-cell liberty file, read via `read_liberty -ignore_miss_func` (no `-lib`, so each cell's liberty `function` string becomes real logic, not a blackbox) before that side's `sources`. **Required for a post-synthesis gate-level netlist** (e.g. `klt synthesize`'s own `netlist_path` output) — without it, the netlist's standard-cell instances have no logic definition and elaboration fails outright. Omit for self-contained RTL. |
| `port_map` | object\<string,string\> \| null | The **I/O mapping** between the two sides, `{"<gate_port_name>": "<gold_port_name>"}` — only needed when a port was renamed between the two representations (e.g. by a synthesis or netlist-rewriting step that does not preserve top-level port names). Ports not listed are assumed identically named on both sides. Omit (or `null`) when both sides already share the same port names — the common case. |
| `engine` | string | `"yosys"` (default; only value implemented). |
| `timeout_s` | number | Overall wall-clock timeout in seconds (default `60`). Overridden by `--timeout-s` when given. Must be positive. |

**State mapping is out of scope for this (combinational) MVP.** A future
sequential phase of #707 will extend this request shape with a register/
state correspondence (analogous to `port_map`, but for `$dff` state
elements) — not needed here since either side containing a register is
already a scope error (see "Scope" above).

## Response

```json
{
  "schema_version": 1,
  "engine": "yosys",
  "engine_version": "0.33",
  "status": "counterexample",
  "gold": { "top": "adder4", "sources": ["/abs/adder4.v"], "liberty": null },
  "gate": { "top": "adder4", "sources": ["/abs/adder4_synth.v"], "liberty": "/abs/lib.lib" },
  "port_map": null,
  "timeout_s": 60.0,
  "elapsed_s": 0.36,
  "counterexample": {
    "inputs": { "a": { "bin": "0000", "width": 4, "value": 0 } },
    "gold_outputs": { "sum": { "bin": "1011", "width": 4, "value": 11 } },
    "gate_outputs": { "sum": { "bin": "1010", "width": 4, "value": 10 } },
    "diverging_outputs": ["sum"],
    "confirmed_by_simulation": true,
    "simulation": {
      "engine": "icarus",
      "engine_version": "12.0",
      "gold_outputs": { "sum": "1011" },
      "gate_outputs": { "sum": "1010" },
      "diverging_outputs": ["sum"]
    }
  },
  "diagnostics": [],
  "artifacts": {
    "script_path": "/abs/.klt/equiv/equiv.ys",
    "netlist_path": "/abs/.klt/equiv/equiv_netlist.v",
    "log_path": "/abs/.klt/equiv/equiv.log"
  },
  "provenance": {
    "klt_version": "0.2.0",
    "klayout_version": "0.30.10",
    "pdk": null,
    "deck": null,
    "input": { "content_hash": "sha256:<hex>" }
  }
}
```

| Field | Type | Description |
| --- | --- | --- |
| `schema_version` | integer | Per-command version, per `docs/json-contract.md`. |
| `engine` / `engine_version` | string | Echo of the request's engine, plus the resolved Yosys build string (`yosys -V`). `engine_version` is `null` if unresolvable. |
| `status` | string | `"equivalent"`, `"counterexample"` (proven non-equivalent), or `"inconclusive"` (solver/process timeout — **never** `"equivalent"`). See "Timeout and the inconclusive verdict" below. |
| `gold` / `gate` | object | Echo of the resolved request side: `{top, sources, liberty}` (absolute paths; `liberty` is `null` when not given). |
| `port_map` | object \| null | Echo of the request field. |
| `timeout_s` | number | The effective timeout used (request field, or `--timeout-s`, or the `60` default). |
| `elapsed_s` | number | Wall-clock time the Yosys subprocess actually ran, in seconds. |
| `counterexample` | object \| null | Present only when `status == "counterexample"`. See "Counterexample shape" below. |
| `diagnostics` | array\<object\> | `{severity, code, message}` entries — a timeout's own explanation, or a degraded (but non-fatal) counterexample-confirmation outcome (e.g. `iverilog` unavailable). Empty on a clean `"equivalent"`/`"counterexample"` run. |
| `artifacts` | object | `{script_path, netlist_path, log_path}` — the generated `.ys` script, the flattened combined `gold`/`gate` Verilog netlist, and the raw Yosys log, all absolute paths under `.klt/equiv/` next to the request file. `netlist_path`/`log_path` are `null` when the run timed out before they were written. Never deleted — kept as debuggable artifacts, the same convention `klt synthesize`'s `.klt/synthesize/` uses. |
| `provenance` | object | The shared envelope block (`docs/json-contract.md`). `pdk`/`deck` are always `null` (no PDK/deck resolution — a liberty file, when given, is a plain input file, not a "deck"); `input` is the content hash of every `sources` file across both sides (a combined, order-independent hash when more than one file is given). |

### Counterexample shape

| Field | Type | Description |
| --- | --- | --- |
| `inputs` | object\<string, object\> | `{name: {bin, width, value}}` for every top-level input port — the concrete vector the SAT solver found (and, on a successful confirmation, the exact vector `iverilog`/`vvp` re-ran). `bin` is the exact-width bit string; `value` is the decoded unsigned integer, or `null` if any bit is undefined. |
| `gold_outputs` / `gate_outputs` | object\<string, object\> | Same `{bin, width, value}` shape, per output port, as reported by the SAT solver for `gold`/`gate` respectively under `inputs`. |
| `diverging_outputs` | array\<string\> | Output port names where `gold_outputs`/`gate_outputs` actually differ (a bus can legally share some bits and diverge on others). |
| `confirmed_by_simulation` | boolean \| null | `true` when `iverilog`/`vvp` independently reproduced the divergence; `false` when the re-simulation ran but did **not** reproduce it (treat the counterexample with suspicion — see `diagnostics`); `null` when confirmation could not be attempted at all (e.g. `iverilog` not installed — see `diagnostics`). |
| `simulation` | object \| null | `{engine, engine_version, gold_outputs, gate_outputs, diverging_outputs}` from the independent `iverilog`/`vvp` run — `gold_outputs`/`gate_outputs` here are raw `{name: bin_string}`, not the richer `{bin, width, value}` shape above. `null` when confirmation could not be attempted. |

## Timeout and the inconclusive verdict

`--timeout-s` (or the request's own `timeout_s`) bounds the whole
`yosys -s <script>` subprocess via Python's own `subprocess.run(...,
timeout=...)` — the same mechanism `klt sim`'s `_run_corner` already uses
for `ngspice`. A run that does not finish in time is **always**
`status: "inconclusive"`, `diagnostics[0].code: "process_timeout"` — this
module's own scope requires a timeout is never reported as
`"equivalent"` (a hung/slow proof says nothing about whether the two
designs actually match). Yosys's own internal `sat -timeout` is also set
to the same budget as a defense-in-depth inner bound (surfaces as
`diagnostics[0].code: "solver_timeout"` when it fires first) — but the
outer process-level timeout above is authoritative and is what every
caller should rely on.

## Exit codes

| Code | Meaning |
| ---- | ------- |
| `0` | Proven equivalent (`status: "equivalent"`). |
| `1` | Failed to run at all — bad request, unresolvable/unreadable RTL or liberty source, unsupported engine, a sequential design (out of this MVP's scope), a Yosys elaboration/miter-construction error, or a missing `yosys` binary. |
| `2` | Usage error (missing argument, bad `--format`/`--timeout-s` value) — from argparse. |
| `3` | Ran successfully, proven non-equivalent (`status: "counterexample"`). |
| `4` | Ran, but the proof is inconclusive — solver or process timeout (`status: "inconclusive"`). Never `0`. |

This is `klt sim`'s 0/1/2/3/4 precedent (not `klt lvs`'s 0/1/2/3): a formal
equivalence proof has the same third "ran, but the result isn't
trustworthy" outcome a PVT corner sweep has (a timeout) — which a binary
netlist comparison (LVS, an exact structural match/mismatch with no solver
involved) does not.

On error (exit `1`), a concise message is written to **stderr** and
nothing is written to stdout. No Python traceback is ever emitted.

- `--format text` (default): a plain-text line prefixed `klt equiv:`.
- `--format json`: the documented JSON error envelope (see
  [`docs/json-contract.md`](../json-contract.md)).

## Worked example

A 4-bit ripple-carry adder (`adder4.v`, combinational — no clock),
synthesized against `gf180mcu_fd_sc_mcu9t5v0`'s typical corner via
`klt synthesize`, then proven equivalent to its own RTL:

```console
$ klt equiv request.json --format json
{
  "schema_version": 1,
  "engine": "yosys",
  "engine_version": "0.33",
  "status": "equivalent",
  ...
}
```

A deliberately seeded-broken mutant (`assign {cout, sum} = a + b;` —
dropping `cin`) synthesized the same way, compared against the same gold
RTL, produces a proven, independently-confirmed counterexample instead
(`status: "counterexample"`, `counterexample.confirmed_by_simulation:
true`) — see `tests/test_equiv.py` for the full request/response pair.

## Out of scope

- **Sequential equivalence.** See "Scope" above — a later phase of #707.
- **State mapping.** Only `port_map` (I/O renaming) is supported; a
  register/state correspondence has no meaning yet since sequential
  designs are rejected outright.
- **SymbiYosys / `sby` orchestration.** See "Engine" above — left for a
  later phase, once sequential scope makes it worth the added dependency.
- **Timing-aware equivalence.** This is a purely functional (Boolean)
  proof; timing is entirely out of scope, matching `klt synthesize`'s own
  `timing: null` posture.
