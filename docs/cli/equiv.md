# `klt equiv`

Prove or refute **combinational** (`"yosys"` engine, Phase 0/1) or
**register-correspondence sequential** (`"yosys-sequential"` engine, Phase
2, #1313) equivalence between two RTL/gate-level netlists (`gold` and
`gate`) via Yosys's own built-in equivalence-checking primitives — part of
the formal-equivalence epic
[#707](https://github.com/2AMLogic/klayout-tools/issues/707), the
correctness loop-closer
[#704](https://github.com/2AMLogic/klayout-tools/issues/704) (RTL
synthesis) and [#700](https://github.com/2AMLogic/klayout-tools/issues/700)
(place-and-route) both name as their own verification step.
[`klt synthesize --verify-equivalence`](synthesize.md#equivalence-gate)
wires this command's combinational engine in directly as `klt synthesize`'s
own acceptance gate (#704 Phase 1) — see that flag's docs for the wired,
one-command version of the "synthesize, then check" flow this document
describes standalone.

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

## Scope

Two engines, both proving/refuting **functional** (Boolean) equivalence —
timing is out of scope for both, see "Out of scope" below.

- **`"yosys"`** (default, Phase 0/1): **combinational only**. A design
  containing flip-flops, latches, or memories on either side is rejected
  up front with a clear scope error (exit `1`) — not silently checked
  per-cycle and reported with a misleadingly confident verdict.
- **`"yosys-sequential"`** (Phase 2, #1313): **register-correspondence
  sequential equivalence** — `gold`/`gate` may contain flip-flops, as long
  as both sides share an identical, 1:1-by-name set of state elements
  (registers). This is the case this repo's own `klt place-and-route`
  pipeline actually, measurably produces (buffer insertion and
  drive-strength resizing only — zero register-count change; see
  `docs/design/sequential-equivalence-survey.md` §2.2). General sequential
  equivalence (retiming, register cloning, FSM re-encoding, where no 1:1
  register correspondence exists) is out of scope for this engine — see
  that survey's §4.4 for the explicitly staged, evidence-gated follow-on.

## Engine

`request.engine` is a data field, not a code path (matching every other
digital-flow verb's own precedent — `synthesize.py`,
`functional_verification.py`) — `"yosys"` and `"yosys-sequential"` are
both implemented. An unsupported value is an application error (exit `1`).

### `"yosys"` (default) — combinational

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

**Why plain Yosys, not SymbiYosys.** SymbiYosys's main value (orchestrating
multi-step *sequential* proofs) is exactly what this MVP's
combinational-only scope does not need — this engine runs the same recipe
SymbiYosys's own `mode bmc`/`mode prove` internally expand to for a
single-cycle proof, orchestrated directly rather than through `sby`.

### `"yosys-sequential"` — register-correspondence sequential

Orchestrates Yosys's own `equiv_make`/`equiv_simple`/`equiv_induct`/
`equiv_status` command family — **not** SymbiYosys (`sby`): as of the
pinned `v0.67` this repo's CI installs (`scripts/install-symbiyosys.sh`,
#1312), `sby` implements only `bmc`/`prove`/`cover`/`live`/`prep` modes —
there is no `sby mode equiv`. The `equiv_make`/`equiv_induct` family are
plain built-in Yosys passes, invoked directly via `yosys -s <script>`, the
same "orchestrate Yosys's own primitives directly" choice the `"yosys"`
engine already made for `miter`/`sat`. Two stages:

1. **Stage 1 (always runs).** `equiv_make` pairs corresponding gold/gate
   wires by name; `clk2fflogic` lowers every clocked flip-flop to Yosys's
   formal-verification `$ff` model (uniform single-/multi-clock,
   sync-/async-reset handling); `equiv_simple` resolves what it can with
   plain per-cell SAT; `equiv_induct` resolves the rest by temporal
   induction over the design's own state elements
   (`request.induction_depth` cycles, default `4`); `equiv_status` reports
   the tally. All cells proven → `status: "equivalent"`.
2. **Stage 2 (only when stage 1 leaves cells unproven).** Register
   correspondence via induction is a *proof* technique, not a
   counterexample-*finding* one — an "unproven" cell honestly means "could
   not decide," not "these differ." Stage 2 rebuilds the same pairing with
   `equiv_make -make_assert` and runs a genuinely bounded (not inductive)
   `sat -seq <induction_depth> -set-init-zero -prove-asserts` search for an
   actual violating trace. `-set-init-zero` anchors both sides to an
   identical, defined starting register state — without it, an
   *unconstrained* free initial state diverges trivially regardless of
   whether the designs actually differ once reachable from reset (a known
   formal-verification pitfall, `docs/design/sequential-equivalence-survey.md`
   §3.5). A violation found within the bound is a **real, demonstrated**
   counterexample; finding none is `status: "inconclusive"` (a bounded,
   all-zero-start search finding nothing does not establish the general,
   unbounded claim induction itself could not prove) — **never** silently
   upgraded to `"equivalent"`.

A genuine stage-2 counterexample is independently confirmed by simulation
exactly as the combinational engine's own counterexample is (see above),
generalised to multi-cycle: the confirmation testbench replays the *entire*
captured cycle-by-cycle input sequence (not a single vector) through the
same flattened gold/gate netlist via `iverilog`/`vvp`, and checks the
solver-reported divergence reproduces *somewhere* in the replayed trace —
not necessarily at the identical cycle index, since a real Verilog
simulation's registers start at `x` (undefined), unlike `-set-init-zero`'s
SAT-side assumption, so the first cycle or two can legitimately disagree on
settling details even for a fully accurate replay.

**`request.induction_depth`** (integer, default `4`, must be positive) —
overrides both stage 1's `equiv_induct -seq` depth and stage 2's `sat -seq`
bound. `4` matches `equiv_induct`'s own Yosys-internal default.

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
| `engine` | string | `"yosys"` (default, combinational) or `"yosys-sequential"` (register-correspondence sequential, #1313). |
| `timeout_s` | number | Overall wall-clock timeout in seconds (default `60`). Overridden by `--timeout-s` when given. Must be positive. Applied independently to *each* stage of the `"yosys-sequential"` engine's two-stage run — a worst-case run may take up to 2x this budget. |
| `induction_depth` | integer | **`"yosys-sequential"` only.** `equiv_induct -seq`/stage-2 `sat -seq` depth (default `4`, matching `equiv_induct`'s own Yosys-internal default). Must be a positive integer. Ignored (no effect) for the `"yosys"` engine. |

**State/register mapping.** The `"yosys"` engine's `port_map` only ever
covers I/O renaming (state mapping is out of scope — either side containing
a register is a scope error). The `"yosys-sequential"` engine needs no
separate register-mapping field: `equiv_make` matches state elements the
same way it matches everything else, by **identical name** — the same
property this repo's own real P&R output preserves (unchanged instance
names across P&R, `docs/design/sequential-equivalence-survey.md` §2.2).
`port_map` still covers top-level I/O renaming for this engine too, applied
before `equiv_make` runs.

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
| `status` | string | `"equivalent"`, `"counterexample"` (proven non-equivalent **and** independently reproduced by simulation), or `"inconclusive"` (solver/process timeout, or a solver-reported counterexample that simulation did *not* reproduce — **never** `"equivalent"`). See "Timeout and the inconclusive verdict" below and "Counterexample shape" for the simulation-confirmation downgrade. |
| `gold` / `gate` | object | Echo of the resolved request side: `{top, sources, liberty}` (absolute paths; `liberty` is `null` when not given). |
| `port_map` | object \| null | Echo of the request field. |
| `timeout_s` | number | The effective timeout used (request field, or `--timeout-s`, or the `60` default). |
| `elapsed_s` | number | Wall-clock time the Yosys subprocess actually ran, in seconds. |
| `counterexample` | object \| null | Present when `status == "counterexample"`, and also (issue #1349) when a solver-reported counterexample was downgraded to `status: "inconclusive"` because `counterexample.confirmed_by_simulation` came back `false` — the object itself is unchanged either way, it is only the top-level `status` that reflects whether the divergence was actually demonstrated. `null` for every other `"inconclusive"` cause (timeout, unproven-by-induction) and for `"equivalent"`. See "Counterexample shape" below. |
| `diagnostics` | array\<object\> | `{severity, code, message}` entries — a timeout's own explanation, or a degraded (but non-fatal) counterexample-confirmation outcome (e.g. `iverilog` unavailable). Empty on a clean `"equivalent"`/`"counterexample"` run. |
| `artifacts` | object | `{script_path, netlist_path, log_path}` — the generated `.ys` script, the flattened combined `gold`/`gate` Verilog netlist, and the raw Yosys log, all absolute paths under `.klt/equiv/` next to the request file. `netlist_path`/`log_path` are `null` when the run timed out before they were written. Never deleted — kept as debuggable artifacts, the same convention `klt synthesize`'s `.klt/synthesize/` uses. |
| `provenance` | object | The shared envelope block (`docs/json-contract.md`). `pdk`/`deck` are always `null` (no PDK/deck resolution — a liberty file, when given, is a plain input file, not a "deck"); `input` is the content hash of every `sources` file across both sides (a combined, order-independent hash when more than one file is given). |

### Counterexample shape

| Field | Type | Description |
| --- | --- | --- |
| `inputs` | object\<string, object\> | `{name: {bin, width, value}}` for every top-level input port — the concrete vector the SAT solver found (and, on a successful confirmation, the exact vector `iverilog`/`vvp` re-ran). `bin` is the exact-width bit string; `value` is the decoded unsigned integer, or `null` if any bit is undefined. |
| `gold_outputs` / `gate_outputs` | object\<string, object\> | Same `{bin, width, value}` shape, per output port, as reported by the SAT solver for `gold`/`gate` respectively under `inputs`. |
| `diverging_outputs` | array\<string\> | Output port names where `gold_outputs`/`gate_outputs` actually differ (a bus can legally share some bits and diverge on others). |
| `confirmed_by_simulation` | boolean \| null | `true` when `iverilog`/`vvp` independently reproduced the divergence (top-level `status` stays `"counterexample"`); `false` when the re-simulation ran but did **not** reproduce it — the top-level `status` is downgraded to `"inconclusive"` in this case (issue #1349: an unproven-`$equiv`/miter artifact is not a demonstrated functional difference), and `diagnostics` carries the `counterexample_not_reproduced` explanation; `null` when confirmation could not be attempted at all (e.g. `iverilog` not installed — see `diagnostics`), in which case `status` is left as the solver's own `"counterexample"` verdict since there is no simulation evidence either way. |
| `simulation` | object \| null | `{engine, engine_version, gold_outputs, gate_outputs, diverging_outputs}` from the independent `iverilog`/`vvp` run — `gold_outputs`/`gate_outputs` here are raw `{name: bin_string}`, not the richer `{bin, width, value}` shape above. `null` when confirmation could not be attempted. |

### Response additions for `"yosys-sequential"`

Additive only — every field above is unchanged for this engine (`gold`/
`gate`/`port_map`/`timeout_s`/`elapsed_s`/`diagnostics`/`provenance`, and
`artifacts.script_path`/`netlist_path`/`log_path` continue to refer to
stage 1, always run):

| Field | Type | Description |
| --- | --- | --- |
| `induction_depth` | integer | Echo of the effective `induction_depth` used (request field, or the `4` default). |
| `artifacts.stage2_script_path` / `artifacts.stage2_log_path` | string \| null | The stage-2 (bounded counterexample search) `.ys` script and raw Yosys log, when stage 2 ran (i.e. stage 1 alone did not reach `"equivalent"`) — `null` when stage 2 never ran. |
| `artifacts.stage1_blacklist_path` | string \| null | The `equiv_make -blacklist` file stage 1's cut-point refinement loop wrote (issue #1353) — one internal wire name per line, the exact set of same-named `gold`/`gate` wires whose pairing was dropped. `null` when the run converged without refinement. Top-level ports are never listed here. |

#### Stage 1 cut-point refinement (issue #1353)

`equiv_make` pairs `gold`/`gate` wires **by name**, and a place-and-route
tool is under no obligation to preserve internal wire names: OpenROAD's
resizing, repair-buffer insertion and gate cloning routinely leave a
same-named internal wire carrying a *different* value on the two sides.
Each such pair becomes an unprovable `$equiv` cell — and, because a
`$equiv` cell is also a **cut point** (both sides' downstream cones read
its output instead of their own driver), a wrongly-paired wire injects a
false assumption into every proof downstream of it. That is why a real
~50-flop post-route netlist used to report `"inconclusive"` even though the
two designs were genuinely equivalent.

Stage 1 therefore re-runs itself as a bounded, counterexample-guided
refinement loop: whatever `equiv_status` reports unproven is fed back into
`equiv_make -blacklist` (minus any top-level port) so no `$equiv` cell —
and so no cut point — is created for those wires at all, then the recipe
runs again. Up to three refinement passes are attempted, all sharing the
single `timeout_s` budget, before falling through to stage 2 as before.

This is **sound, and strictly stronger than not doing it**: blacklisting
only ever *removes* assumptions, so every surviving obligation is proven
from more of the two designs' real logic. Top-level ports are never
blacklisted, so "`gold` and `gate` produce the same outputs" is always
still proven, never dropped — a genuinely-broken `gate` netlist simply
fails to converge and stage 2's bounded search runs as before. When
refinement ran, `diagnostics` carries an `equiv_cutpoint_refinement` info
entry with the count and `artifacts.stage1_blacklist_path` records the
exact list, so the weakened obligation set can be audited rather than
taken on trust.

### Sequential counterexample shape (`"yosys-sequential"` only)

Present when `status == "counterexample"`, and also — exactly as the
combinational shape above — when a stage-2 counterexample was downgraded to
`status: "inconclusive"` because `confirmed_by_simulation` came back `false`
(issue #1349). Replaces the single-vector shape above with a **multi-cycle**
one, generalising it for a genuinely sequential trace:

```json
{
  "cycles": [
    { "time": 1, "inputs": { "clk": {"bin":"0","width":1,"value":0}, "d": {"bin":"1","width":1,"value":1} },
      "gold_outputs": { "q": {"bin":"0","width":1,"value":0} },
      "gate_outputs": { "q": {"bin":"0","width":1,"value":0} },
      "diverging_outputs": [] },
    { "time": 2, "inputs": { "clk": {"bin":"1","width":1,"value":1}, "d": {"bin":"1","width":1,"value":1} },
      "gold_outputs": { "q": {"bin":"1","width":1,"value":1} },
      "gate_outputs": { "q": {"bin":"0","width":1,"value":0} },
      "diverging_outputs": ["q"] }
  ],
  "diverging_outputs": ["q"],
  "first_diverging_cycle": 2,
  "confirmed_by_simulation": true,
  "simulation": {
    "engine": "icarus",
    "engine_version": "12.0",
    "cycles": [
      { "time": 1, "gold_outputs": {"q": "x"}, "gate_outputs": {"q": "x"}, "diverging_outputs": [] },
      { "time": 2, "gold_outputs": {"q": "1"}, "gate_outputs": {"q": "0"}, "diverging_outputs": ["q"] }
    ],
    "diverging_outputs": ["q"]
  }
}
```

| Field | Type | Description |
| --- | --- | --- |
| `cycles` | array\<object\> | One entry per captured cycle (`induction_depth` entries), each the same `{time, inputs, gold_outputs, gate_outputs, diverging_outputs}` shape a single combinational vector has (see "Counterexample shape" above), plus `time` (the 1-indexed cycle number). |
| `diverging_outputs` | array\<string\> | The union of every cycle's own `diverging_outputs` — every output that diverged on *any* captured cycle. |
| `first_diverging_cycle` | integer \| null | The `time` of the earliest cycle with a nonempty `diverging_outputs`. |
| `confirmed_by_simulation` | boolean \| null | Same meaning (and same `status`-downgrade-to-`"inconclusive"`-on-`false` behavior, issue #1349) as the combinational shape, but "confirmed" means the reported divergence reproduces on *some* replayed cycle, not necessarily the identical cycle index — see the `"yosys-sequential"` engine's own "Engine" section above for why (a real Verilog simulation's registers start at `x`, unlike the solver's own `-set-init-zero` assumption). |
| `simulation.cycles` | array\<object\> | The independent `iverilog`/`vvp` run's own per-cycle `{time, gold_outputs, gate_outputs, diverging_outputs}` (raw `{name: bin_string}` values, not the richer `{bin, width, value}` shape). |
| `simulation.diverging_outputs` | array\<string\> | The union of every simulated cycle's own divergence. |

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
| `1` | Failed to run at all — bad request, unresolvable/unreadable RTL or liberty source, unsupported engine, a sequential design given to the `"yosys"` engine (out of its combinational-only scope), a Yosys elaboration/miter-construction error, or a missing `yosys` binary. |
| `2` | Usage error (missing argument, bad `--format`/`--timeout-s` value) — from argparse. |
| `3` | Ran successfully, proven non-equivalent (`status: "counterexample"`). |
| `4` | Ran, but the proof is inconclusive — solver/process timeout, an induction-bound proof with no confirming or refuting counterexample, or a solver-reported counterexample that simulation did not reproduce (`status: "inconclusive"`, issue #1349). Never `0`. |

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

A register-preserving pipeline stage (`gold`) compared against a
buffer-inserted mutation of the same design (`gate`, a real P&R-shaped
transformation — see "Scope" above) with `"engine": "yosys-sequential"`:

```console
$ klt equiv seq_request.json --format json
{
  "schema_version": 1,
  "engine": "yosys-sequential",
  "engine_version": "0.67",
  "status": "equivalent",
  "induction_depth": 4,
  ...
}
```

A deliberately seeded-broken mutant (a D-input polarity inversion feeding
the same register) instead produces a proven, independently-confirmed
multi-cycle counterexample (`status: "counterexample"`,
`counterexample.confirmed_by_simulation: true`) — see
`tests/test_equiv.py`'s `yosys-sequential` engine tests for the full
request/response pair.

## Re-running the real pre/post-route canary

Whether `"yosys-sequential"` finds a real, live `klt place-and-route`
transformation `"equivalent"` (not just the buffer-inserted synthetic mutant
above) is exercised by two real-toolchain tests in
`tests/test_equiv.py`: `test_sequential_engine_real_pnr_register_preserving_transformation`
(GCD — `klt synthesize`'s pre-route netlist from
`examples/functional-verification/gcd.v`, the same GCD source of truth
`tests/corpus/place_and_route/regenerate.sh` uses, vs. the real, post-route
`verilog_path` `klt place-and-route` produces) and
`test_sequential_engine_real_pnr_mult8_register_preserving_transformation`
(the `mult8` corpus design, added by issue #1353 to verify its own fix
against a second design) — both via a real `openroad` binary and a real
sky130 PDK install, issue #1313's acceptance criterion. Like
`test_place_and_route.py`'s own live-`openroad` integration tests, both are
`skipif`-guarded on `yosys`/`openroad`/a real sky130 PDK all being present,
so they are silently skipped — not run — in this repo's ordinary `ci.yml`
`test` job.

> **Status (2026-08-24, issue #1353): both canaries report `"equivalent"`
> against the real toolchain, and neither carries an `xfail` marker any
> more.** Verified live on `openroad` 26Q3-1510-g6cb3f2b704
> (`openroad/orfs:latest`), a real pinned volare `sky130A` install, and this
> repo's pinned Yosys `0.67+post`:
>
> - The GCD P&R transformation *is* register-preserving (50
>   `sky130_fd_sc_hd__dfrtp_1` flops on both sides).
> - Stage 1's **first** pass leaves 93 of 1521 `$equiv` cells unproven (98
>   without the engine's `opt -noff` normalization pass — a controlled A/B on
>   one netlist pair). Every one of those 93 is a same-named *internal* wire
>   OpenROAD's resizer/repair-buffer/cloning passes repurposed, not an output
>   difference.
> - Stage 1's cut-point refinement loop (see "Stage 1 cut-point refinement"
>   above) blacklists exactly those 93 wires and re-runs: **1428 of 1428
>   `$equiv` cells proven, `"Equivalence successfully proven!"`, one
>   refinement pass, ~28 s total.** The reported verdict is `"equivalent"`,
>   with an `equiv_cutpoint_refinement` info diagnostic and the blacklist
>   itself kept at `artifacts.stage1_blacklist_path`.
>
> Historically this canary reported a false `status: "counterexample"` (fixed
> by #1349, which replays the stage-2 trace through `iverilog`/`vvp` and
> downgrades an unreproduced trace to `"inconclusive"`), then `"inconclusive"`
> until #1353 closed the proof-strength gap.
>
> **Negative control, run the same way:** mutating a single cell in that same
> real post-route netlist (`sky130_fd_sc_hd__nand2_1 _382_` →
> `..._nor2_1`) does *not* converge — refinement exhausts its three passes
> and the run reports `"inconclusive"`, never `"equivalent"`. Refinement
> drops only internal wires, so a real functional break cannot be laundered
> into a pass.
>
> `mult8` also reports `"equivalent"`, though it does not itself exercise the
> refinement loop: OpenROAD's route-stage output for that design is
> instance-for-instance identical to its pre-route netlist (same 260 cells,
> same instance names), so it already converged before #1353. It is kept as a
> second-design regression guard over the whole `synthesize` →
> `place-and-route` → `equiv` path.
>
> **Independently reconfirmed (2026-08-24, issue #1323) against `origin/main`
> post-#1360-merge** — a fresh `pytest tests/test_equiv.py -k
> register_preserving_transformation -v -rs -s` run (not a citation of #1360's
> own numbers) against `openroad 26Q3-1510-g6cb3f2b704`
> (`openroad/orfs:latest`) and a real pinned volare `sky130A` install:
> `2 passed, 79 deselected` — both the GCD and `mult8` canaries report
> `status: "equivalent"`, confirmed not `SKIPPED` and not `XPASS` (no `xfail`
> marker present). This closes epic #707's acceptance criterion 3 for the
> P&R half; see issue #1323 and epic #707 for the full evidence trail.

[`.github/workflows/equiv-canary.yml`](../../.github/workflows/equiv-canary.yml)
(issue #1324, Epic #707 Phase 3) gives that canary a repeatable, `CI`-runnable
execution path: it provisions `openroad` and a full sky130A PDK the same way
[`place-and-route-smoke.yml`](../../.github/workflows/place-and-route-smoke.yml)
already does for Epic #700 (see `docs/cli/place-and-route.md`'s "CI"
section — the provisioning steps are reused, not reinvented), then runs the
canary test and asserts it actually *ran* — a skipped canary is failed, not
passed, so a missing binary or PDK can never masquerade as a green verdict.
It is `workflow_dispatch`-only, for the identical reason
`place-and-route-smoke.yml` is: the Docker/PDK pull is multi-GB of one-time
cost on a cache miss, too heavy to gate every PR. Trigger it manually from
the Actions tab, or `gh workflow run equiv-canary.yml`, whenever `klt
equiv`'s `"yosys-sequential"` engine, `klt synthesize`, or `klt
place-and-route` change, or before a release, to check the verdict against a
real OpenROAD/sky130 pipeline — distinct from the fixture-based
`"yosys-sequential"` unit tests that already run in ordinary CI.

## Out of scope

- **General sequential equivalence.** The `"yosys-sequential"` engine
  proves *register-correspondence* equivalence only (§"Scope" above) —
  retiming, register cloning, and FSM re-encoding (cases with no 1:1
  register correspondence) are explicitly out of scope for it; a later,
  evidence-gated phase of #707 covers bounded/k-induction model checking
  for those cases (`docs/design/sequential-equivalence-survey.md` §4.4).
- **State mapping beyond identical naming.** `equiv_make` matches state
  elements the same way it matches everything else: by identical name.
  There is no separate register-remapping field analogous to `port_map`
  for a design whose register *names* differ between `gold`/`gate`.
- **Timing-aware equivalence.** This is a purely functional (Boolean)
  proof; timing is entirely out of scope, matching `klt synthesize`'s own
  `timing: null` posture.
