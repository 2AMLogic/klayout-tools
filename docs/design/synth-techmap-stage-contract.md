# Contract: the technology-mapping stage (`klt synth` Phase 2a)

**Status:** contract proposal. Nothing here authorises implementation — no
dependency was added, no `klt` subcommand or native crate was written, and
no code in `src/klayout_tools/` or `native/` changed as part of this
document, matching
[`digital-flow-contracts-spike.md`](digital-flow-contracts-spike.md)'s own
posture at the same point in Epic #391. This is
[Epic #704](https://github.com/2AMLogic/klayout-tools/issues/704) ("RTL
synthesis for klt (Rust) — Verilog→technology-mapped gate netlist, Yosys as
oracle") Phase 2a, issue #873: define the technology-mapping stage's JSON
input/output contract — generic netlist + Liberty in, mapped gate netlist +
area/delay report out — before any mapping algorithm is implemented. Phase 1
(QoR improvements to the existing Yosys+abc-orchestrated flow, plus wiring
`klt equiv` as its acceptance gate) is complete; this phase begins the
native technology-mapping stage Phase 2's own epic description names.

**Required prior art, read first, not re-derived here:**

- [`docs/design/digital-flow-contracts-spike.md`](digital-flow-contracts-spike.md)
  (#399) — the contract-first template this document follows (Request /
  Response / exit codes / build-wrap decision / engine-agnostic check), and
  the already-shipped `klt synthesize` / `klt place-and-route` contracts
  this stage's own request/response fields deliberately reuse vocabulary
  from.
- [`docs/cli/synthesize.md`](../cli/synthesize.md) — `klt synthesize`'s own
  request/response contract (`instance_count`, `area_um2`,
  `instance_counts_by_type`, `timing`, `netlist_path`), the field names this
  stage's own response echoes rather than inventing a parallel vocabulary.
- [`docs/cli/place-and-route.md`](../cli/place-and-route.md) — `klt
  place-and-route`'s `netlist` request field, the downstream consumer this
  stage's `mapped_netlist_path` must satisfy (checked in §5 below).
- [`docs/design/synthesize-qor-improvements-survey.md`](synthesize-qor-improvements-survey.md)
  (#748) §3.7 ("Sketch B") and `native/statime/README.md` (issue #809,
  **Go**) — the accepted native-Rust gate-level STA spike this stage's
  `timing` field is designed to plug into once a later issue wires it in.
- `native/statime/src/netlist.rs`'s own docstring — the exact structural-
  Verilog dialect a real consumer (this crate) already parses successfully
  against three corpus designs; this stage's `mapped_netlist_path` is
  designed to conform to that same dialect rather than a new one.
- [`docs/ARCHITECTURE.md`](../ARCHITECTURE.md) "Rewrite rule" — the
  bottleneck/oracle/unlock test §7 below applies.

## 1. Where this stage sits

```
RTL sources
  │  (klt synthesize today: Yosys `synth`, or a future native elaborator)
  ▼
optimized generic netlist  ──┐
                              │  Liberty (.lib)
                              ▼
                    ┌───────────────────┐
                    │ technology mapping│   <- this contract (Phase 2)
                    └───────────────────┘
                              │
                              ▼
                    mapped gate netlist + area/delay report
                              │
                              ▼
                 klt place-and-route (#700) `netlist` input
```

Today, `klt synthesize` produces a mapped netlist directly by orchestrating
Yosys + its bundled ABC as one subprocess — there is no separate
"technology mapping" step a caller can invoke or inspect on its own. This
contract defines that step as its own JSON-in/JSON-out interface so it can
be built, tested, and benchmarked against Yosys/ABC independently (per the
epic's oracle-grounding discipline — see §7) before it is wired into any
`klt` command. It is deliberately **not yet a `klt` CLI verb**: like
`native/statime/`'s own `klt-statime critical-path` binary (issue #809),
the crate this contract specifies (`native/techmap/`, landing in #874) gets
its own standalone CLI for corpus benchmarking first; wiring it into
`klt synthesize`/a future `klt synth` and gating it with `klt equiv` is
#875's job, not this issue's.

## 2. Input: the generic netlist

### 2.1 Why a new format, not Yosys's `write_json`

`digital-flow-contracts-spike.md` §4/§5 deliberately kept `klt synthesize`'s
own request/response contract free of Yosys-specific vocabulary ("no field
is named after a Yosys pass... or shaped like Yosys's own JSON") so a future
second synthesis engine is an additive `engine` enum value, not a
contract-shape change. The same reasoning applies here, more so: this stage
is the one Phase 2/3 intend to **replace** Yosys/ABC's own technology
mapper with, so its input format cannot be Yosys's internal `write_json`
connectivity graph (RTLIL-as-JSON, `$_AND_`/`$_DFF_P_`-style cell names,
6,475 lines for a 335-cell design per the Yosys survey's own measurement) —
that would make "native technology mapping" permanently dependent on Yosys
having run first, defeating Phase 1/3's stated goal of a native front end.

Instead, `klt.synth.generic-netlist/1` (below) is a small, engine-agnostic,
technology-independent gate netlist format: the target this stage's future
sibling (a native elaboration/logic-optimization stage, not yet built) is
expected to emit, and the same format `klt equiv`/`klt place-and-route`'s
own contracts would extend to if a future phase needs one. **[PROPOSAL]**
— this is this document's own design choice, not a measurement.

### 2.2 Generic cell vocabulary

A fixed, documented set of technology-independent primitives — deliberately
minimal (any 2-input Boolean function decomposes into `$and2`/`$or2`/`$not`;
any real design's flip-flops decompose into the one generic `$dff`), the
same "small universal library" precedent conventional technology mappers
target before technology-dependent covering:

| `type` | Pins | Function |
| --- | --- | --- |
| `$not` | `A` → `Y` | `Y = !A` |
| `$buf` | `A` → `Y` | `Y = A` |
| `$and2` | `A`, `B` → `Y` | `Y = A & B` |
| `$nand2` | `A`, `B` → `Y` | `Y = !(A & B)` |
| `$or2` | `A`, `B` → `Y` | `Y = A \| B` |
| `$nor2` | `A`, `B` → `Y` | `Y = !(A \| B)` |
| `$xor2` | `A`, `B` → `Y` | `Y = A ^ B` |
| `$xnor2` | `A`, `B` → `Y` | `Y = !(A ^ B)` |
| `$mux2` | `A`, `B`, `S` → `Y` | `Y = S ? B : A` |
| `$dff` | `D`, `CLK` → `Q`; optional `RST`, `EN` | Positive-edge D flip-flop. `RST` (if present) is an active-high, asynchronous, active state `Q=0`. `EN` (if present) gates whether `D` is captured on the active clock edge. |

Deliberately close to (but not literally) Yosys's own `simplemap`-produced
`$_AND_`/`$_DFF_P_` simple-cell set — see §2.4 for the interim on-ramp this
similarity buys before a native elaboration stage exists. All ten types are
`v1`; a design using a construct outside this set (multi-clock-domain
sequential elements, latches, tri-state drivers, scan chains) is out of
scope for `v1` — see §8.

### 2.3 Schema

Published as
[`docs/schemas/synth-generic-netlist.schema.json`](../schemas/synth-generic-netlist.schema.json)
(`klt.synth.generic-netlist/1`). One flat module — no sub-module
instantiation — matching the same "flat module, no hierarchy" scope
`native/statime/src/netlist.rs`'s mapped-netlist dialect already commits to
one stage downstream, and that `klt synthesize`'s own `synth -top <top>`
pass already produces by the time `write_verilog` runs.

```json
{
  "schema": "klt.synth.generic-netlist/1",
  "top": "adder4",
  "ports": [
    { "name": "a", "direction": "input", "bits": ["a[3]", "a[2]", "a[1]", "a[0]"] },
    { "name": "b", "direction": "input", "bits": ["b[3]", "b[2]", "b[1]", "b[0]"] },
    { "name": "sum", "direction": "output", "bits": ["sum[3]", "sum[2]", "sum[1]", "sum[0]"] },
    { "name": "cout", "direction": "output", "bits": ["cout"] }
  ],
  "cells": [
    { "name": "_g0_", "type": "$xor2", "connections": { "A": "a[0]", "B": "b[0]", "Y": "sum[0]" } },
    { "name": "_g1_", "type": "$and2", "connections": { "A": "a[0]", "B": "b[0]", "Y": "n1" } }
  ]
}
```

Net names follow the same `name` (scalar) / `name[index]` (bus bit) bit
naming `native/statime/src/netlist.rs`'s `expand_bus` already establishes
for the *mapped*-netlist dialect one stage downstream — the same net-naming
vocabulary applies on both sides of this stage, so a caller never
re-derives one from the other.

| Field | Type | Description |
| --- | --- | --- |
| `schema` | string | `"klt.synth.generic-netlist/1"`. |
| `top` | string | The design's top module name. |
| `ports[]` | array\<object\> | `{name, direction ("input"\|"output"\|"inout"), bits}` — `bits` is the ordered list of individual net names this port expands to (MSB-first, matching Verilog `[N:0]` declaration order). `"inout"` is accepted structurally for tri-state I/O pads but unmapped by `v1` (§8). |
| `cells[]` | array\<object\> | `{name, type, connections}` — `type` is one of §2.2's ten primitives; `connections` maps each of that primitive's pin names to a net name from `ports[].bits` or an internal net not declared elsewhere (an implicit wire, matching Yosys's own `write_verilog` net-declaration behaviour). |

Constant-valued nets (tie-hi/tie-lo, e.g. an unused input tied to `0`) are
an **open question**, not resolved by `v1` — see §8.

### 2.4 Interim on-ramp before a native elaborator exists

Phase 1 (#748/#822/#830) improved the existing Yosys+ABC-orchestrated flow;
it did not build a native elaboration/logic-optimization stage, so nothing
in this repo emits `klt.synth.generic-netlist/1` yet. Until a native
elaborator exists, #874's own benchmarking corpus (mirroring
`native/statime/`'s `tests/corpus/statime/regenerate.sh` precedent) can
produce one mechanically: `synth -top <top> -run coarse:fine; techmap -map
+/simplemap.v; opt; write_json`, then a small converter (not part of this
contract; a test-fixture generator #874 owns) mapping Yosys's
`$_AND_`/`$_OR_`/`$_NOT_`/`$_DFF_P_`/… cells 1:1 onto §2.2's vocabulary —
the same "generate a real fixture with the oracle tool, spot-check by hand"
approach `native/statime/README.md`'s own corpus regeneration already uses.
This keeps #874 unblocked without requiring Phase 1/3's native front end to
land first, and keeps the oracle-grounding discipline intact: the *same*
generic netlist this stage maps is derivable from Yosys's own coarse
synthesis output, so a side-by-side comparison against Yosys/ABC's own
technology mapping on identical logic is possible from day one.

## 3. Input: Liberty

Same Liberty file a resolved `pdk.cell_library`/`corner` pair already
names via `find_pdk()`/`libs_ref` discovery (`docs/cli/synthesize.md`
"PDK / liberty resolution") — **this stage does not re-implement PDK
resolution.** It takes an already-resolved `.lib` file **path**, exactly
the way `native/statime`'s own `klt-statime critical-path <netlist.v>
<liberty.lib>` CLI already takes a liberty path rather than a
`cell_library`/`corner` selector pair (`native/statime/src/main.rs`). PDK
resolution stays a Python-layer concern (`src/klayout_tools/pdk.py`); every
native crate at `native/<engine>/` takes resolved paths, never re-derives
them — the same boundary `native/mom/`, `native/legalize/`, and
`native/statime/` already draw.

Parsed the same way `native/statime/src/liberty.rs` already does: cell
`area`, pin `direction`/`capacitance`, and `timing()` NLDM arcs — single
corner per invocation, no multi-corner `operating_conditions` selection,
matching the single-`.lib`-file scope `abc -liberty` already has today.

## 4. Output: the mapped netlist

`mapped_netlist_path` is structural Verilog in **exactly** the dialect
`native/statime/src/netlist.rs`'s own docstring specifies and already
parses successfully (three real corpus designs, `native/statime/README.md`
§"Results — accuracy"): a flat module, `input`/`output`/`wire`
declarations, named-port (`.PIN(net)`) technology-cell instances only — no
`assign`, no concatenation, no inline constants (a constant-valued net is
resolved to a tie-cell instance by this point, the same way `abc -liberty`'s
own output already has none by the time `write_verilog -noattr` runs
today). This is a **repeat of an existing, already-verified dialect**, not
a new one invented for this contract — see §5 for why that matters.

## 5. Compatibility check against `klt par`'s netlist input

Acceptance criterion 2 (issue #873): *"The contract's mapped-netlist shape
is compatible with `klt par`'s (#700) netlist input."* Checked directly,
against both existing consumers in this repo, neither of which needs a new
parser written for this stage's output:

- **`klt place-and-route`'s `netlist` request field**
  (`docs/cli/place-and-route.md` "Request" table): *"The gate-level netlist
  path — typically `klt synthesize`'s own `netlist_path` output."* OpenROAD
  reads it via `read_verilog`/`link_design` against the resolved cell LEF —
  a plain structural-Verilog reader with no stronger requirement than §4
  already satisfies (flat module, named-port cell instances, real
  technology-cell types resolvable against the linked LEF/liberty).
- **`native/statime/src/netlist.rs`** — already parses this exact dialect
  end to end today, independently of this issue, against `gcd`/`mult8`/
  `modexp` (`native/statime/README.md`). A `mapped_netlist_path` this
  stage emits is drop-in readable by that crate with **zero code change**,
  which is the strongest evidence available short of writing the mapper
  itself: a real, already-shipped consumer already accepts the dialect
  this contract specifies.

Both checks pass by construction, because §4 does not define a new dialect
— it names the one dialect two independent, already-existing consumers in
this repo already agree on.

## 6. Full request/response contract

### Request — `klt.synth.techmap.request/1`

```json
{
  "schema": "klt.synth.techmap.request/1",
  "generic_netlist": "adder4_generic.json",
  "liberty": "/abs/path/sky130_fd_sc_hd__tt_025C_1v80.lib",
  "constraints": { "clock_period_ns": null }
}
```

| Field | Type | Description |
| --- | --- | --- |
| `schema` | string | `"klt.synth.techmap.request/1"`. Not validated — same convention as `klt synthesize`'s own `request.schema` (`docs/cli/synthesize.md`). |
| `generic_netlist` | string | Path to a `klt.synth.generic-netlist/1` file (§2). Resolved the same way a request document's own relative paths resolve elsewhere in this repo — against the request file's own directory. Required. |
| `liberty` | string | Path to a resolved `.lib` file (§3). Required — this stage does not resolve a PDK itself. |
| `constraints.clock_period_ns` | number \| null | The target clock period in nanoseconds — the same field name/meaning `klt synthesize`'s own `constraints.clock_period_ns` already carries (`docs/cli/synthesize.md`), threaded through as this stage's own delay target for area/delay-driven cell selection (#874's algorithm). `null`/omitted: map for area only, no delay target. |

### Response — `klt.synth.techmap.response/1`

```json
{
  "schema_version": 1,
  "top": "adder4",
  "status": "ok",
  "instance_count": 42,
  "area_um2": 412.5,
  "instance_counts_by_type": {
    "sky130_fd_sc_hd__and2_1": 4,
    "sky130_fd_sc_hd__xor2_1": 4
  },
  "timing": null,
  "mapped_netlist_path": "/abs/path/adder4_mapped.v"
}
```

| Field | Type | Description |
| --- | --- | --- |
| `schema_version` | integer | Per-contract version, per `docs/json-contract.md`'s convention. |
| `top` | string | Echo of the request's generic netlist `top`. |
| `status` | string | Always `"ok"` — same "no pass/fail concept, a failed run never emits this envelope" posture `klt synthesize`'s own `status` field already documents. |
| `instance_count` | integer | Total mapped standard-cell instances. Same name/meaning as `klt synthesize`'s own `instance_count` (`docs/cli/synthesize.md`) — deliberately not a new field name for the same concept. |
| `area_um2` | number | Total mapped area, in µm² (the Liberty's own unit) — same name/meaning as `klt synthesize`'s `area_um2`. |
| `instance_counts_by_type` | object\<string, int\> | Per-cell-type instance counts, keys sorted — same shape as `klt synthesize`'s own field. |
| `timing` | object \| null | `{source, critical_path_ps, delay_target_ps}` once a delay-aware engine is wired in (`source: "statime"`, per the accepted `native/statime` spike, issue #809) — or `null` until that wiring lands (§8). `delay_target_ps` echoes `constraints.clock_period_ns × 1000` when given, `null` otherwise — matching `klt synthesize`'s own `timing.delay_target_ps` semantics exactly. |
| `mapped_netlist_path` | string | The mapped gate-level netlist, absolute path — see §4/§5. |

#### `equivalence` — the acceptance gate's own field (#875)

`klayout_tools.techmap.run_techmap()` (the Python runner that invokes the
`klt-techmap` binary) adds one **additive** field to the response above:
`equivalence`. It is `null` by default and, when the caller passes
`verify_equivalence=True`, carries the `klt equiv` proof summary
(`{status, engine, engine_version, timeout_s, elapsed_s,
generic_netlist_verilog_path, artifacts}`) for the proof that the mapped
netlist is logically equivalent to the *pre-mapping generic netlist*
(§2) it was produced from. A non-`"equivalent"` verdict is never returned
— it raises, so a mapped netlist the gate could not prove faithful is
never accepted. The `klt-techmap` binary itself is unchanged and never
emits this field.

Scope: `klt equiv`'s Phase 0 MVP is combinational-only, so a generic
netlist containing §2.2's `$dff` cannot be gated today — the gate rejects
it up front with a scope error naming #707 rather than emitting sequential
Verilog for Yosys to reject less legibly. Same limitation `klt synthesize
--verify-equivalence` already carries at the RTL stage.

### Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Mapping succeeded; every generic cell realized by some Liberty cell, `mapped_netlist_path` written. |
| `1` | Failed to map — malformed `generic_netlist` (fails §2.3's schema), a `cells[].type` outside §2.2's `v1` vocabulary, unreadable/unparseable Liberty, or a generic cell with no realizable Liberty equivalent at all (e.g. a Liberty library missing a basic gate function entirely — not merely "not the cheapest choice", which is a QoR question for #874's algorithm, not a hard failure). |
| `2` | Usage error (missing required request field). |

No exit code `3` — same reasoning as `klt synthesize`'s own "no code 3"
note (`docs/cli/synthesize.md` "Exit codes"): technology mapping either
produces a netlist or it fails, with no separate "ran but found problems"
outcome of its own.

## 7. Build/wrap decision

Per `docs/ARCHITECTURE.md`'s rewrite rule, all three conditions:

1. **Bottleneck or ceiling.** Today's technology mapping is entirely
   ABC's, reached only through the three flags Phase 1 already exhausted
   (`-constr`/`-D`/`-dont_use`, `docs/design/synthesize-qor-improvements-survey.md`
   §3.1–§3.3) — there is no further native knob to turn on a wrapped ABC
   subprocess. Area/delay-driven cell selection tunable from `klt` itself
   (the epic's own Phase 2 goal) is a capability ceiling on the wrapped
   approach, not a QoR gap closeable by more flags.
2. **Oracle exists.** `klt synthesize` (Yosys + ABC) already produces a
   mapped netlist from the same RTL on the same Liberty — the side-by-side
   comparison §2.4 describes is available from day one, and is exactly
   #874's own acceptance criterion ("benchmarked against Yosys/abc").
3. **Unlock.** A JSON-native, engine-agnostic technology-mapping stage
   composes with a future native elaboration stage (Phase 1/3) and exposes
   the tunable cell-selection scoring function #874 implements as a first-
   class `klt`-owned surface — not reachable by wrapping ABC's opaque
   internal mapper, which exposes exactly the three flags named above and
   no scoring hook.

**Where it lives:** `native/techmap/` (new crate, following
`native/statime/`'s spike-crate precedent: plain `cargo build`/`cargo test`
binary crate, `[[bin]]` CLI + `[lib]`, no `pyo3`/`maturin` wiring until a
follow-on issue's own Go verdict, per `native/legalize/README.md`'s
"deliberately... no `pyo3`... adding the production-integration surface
before a 'Go' verdict would spend real maintenance cost" reasoning). This
issue creates no crate — #874 does.

## 8. Open questions carried forward (not resolved here)

- **Constant nets — resolved by #874.** A tie-hi/tie-lo net is represented
  as a **reserved net name**, not a dedicated pseudo-cell: `"$const0"`/
  `"$const1"` may appear anywhere a real net name is valid in
  `cells[].connections` (the schema's `connections` field already accepts
  any non-empty string, so this needed no schema-shape change — only this
  documented convention). `native/techmap`'s `map.rs` resolves each
  reference it finds to a single shared instance of the cheapest liberty
  cell whose output `function` text is the literal `"0"`/`"1"`
  (`sky130_fd_sc_hd__conb_1`'s `LO`/`HI` pins) — a real tie cell, not a
  directly-wired power rail, which is not a valid logic net for a
  standard-cell input pin. See `native/techmap/README.md` "Constant nets"
  for the full reasoning and `tests/corpus/techmap/yosys_to_generic.py`
  for the interim converter emitting this convention from Yosys's own
  `"0"`/`"1"` constant bit markers.
- **`$dff` variants.** §2.2's generic flip-flop is deliberately minimal
  (single global clock, active-high async reset, optional enable) — no
  synchronous reset, no active-low polarity, no scan. Whether `v1` needs
  richer sequential primitives is deferred until a real corpus design
  (Tiny Tapeout, #520) demonstrates the gap; `native/statime`'s own three
  corpus designs are single-clock and did not need more.
- **`timing` population.** `null` until a follow-on issue wires
  `native/statime` (issue #809, accepted **Go**) in as this stage's own
  delay estimator for cell-selection scoring — not this issue's scope.
- **`v1` scope gaps.** Latches, tri-state drivers/`inout` ports, and
  multi-clock-domain designs are out of scope for `v1` (§2.2/§2.3) — same
  "state the gap plainly, don't paper over it" discipline
  `docs/cli/synthesize.md`'s own "Out of scope" section already follows for
  sequential `klt equiv`.

## 9. Related

- [Epic #704](https://github.com/2AMLogic/klayout-tools/issues/704) — RTL
  synthesis for `klt`.
- #874 — implement Liberty-driven cell selection for technology mapping,
  benchmarked against Yosys/abc, against this contract.
- #875 — wire `klt equiv` as this stage's own acceptance gate
  (**shipped**: `src/klayout_tools/techmap.py`, §6's `equivalence` field,
  proven in `tests/test_techmap_equiv_gate.py` against both a real mapping
  and a seeded mismatch).
- #809 / `native/statime/` — the accepted native-Rust gate-level STA spike
  this stage's `timing` field is designed to plug into.
- #700 — `klt place-and-route`, the consumer this stage's
  `mapped_netlist_path` feeds (§5).
