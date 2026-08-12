# Spike: the three digital-flow JSON contracts (synthesis / place-and-route / functional verification)

**Status:** spike / contract proposal. Nothing here authorises implementation
— no dependency was added, no `klt` subcommand was written, and no code in
`src/klayout_tools/` changed as part of this document. This is Phase 1's
final deliverable for [Epic #391](https://github.com/2AMLogic/klayout-tools/issues/391)
("adopt the digital engine class — Yosys + OpenROAD — RTL→GDS as a first-class
`klt` flow"), specifically issue #399: the downstream synthesis step that
turns Phase 1's three parallel engine surveys —
[docs/design/yosys-synthesis-spike.md](yosys-synthesis-spike.md) (#396),
[docs/design/openroad-invocation-survey.md](openroad-invocation-survey.md)
(#397), and
[docs/design/cocotb-verification-spike.md](cocotb-verification-spike.md)
(#398) — into the three proposed JSON contracts named in the epic's own
"Contracts to spike" section. Per
[docs/ARCHITECTURE.md](../ARCHITECTURE.md) → "How capabilities arrive," those
three surveys were the candidate-engine survey step; this document is the
"propose the JSON contract" step that must land before any build phase
starts, per Epic #391's own Phase 1 → Phase 2 gate and Success Criteria
("the three JSON contracts... are documented and reviewed before any
implementation phase starts").

**This document performs no new tool invocation.** Every field below is
either taken directly from one of the three merged surveys' own worked-example
output, or derived by applying this repo's already-established contract
conventions (`docs/json-contract.md`, and the field-naming precedent in
`klt drc`/`klt lvs`/`klt extract`/`klt sim`/`klt eval`) to the surveys'
findings. Where a survey flagged an open question (Yosys's timing gap,
OpenROAD's incomplete routing run, cocotb's already-complete contract), this
document states the resolution or explicitly defers it — it does not
re-verify anything the source surveys already verified live.

## 0. What the three surveys already settled, in one place

Recapping only what each contract below depends on — see the source documents
for the full evidence:

| Survey | Engine(s) | Load-bearing finding this document builds on |
| --- | --- | --- |
| #396 → `yosys-synthesis-spike.md` | Yosys 0.67 + bundled ABC | Script-mode invocation (`yosys -s <script.ys>`); `stat -liberty <lib> -json -top <top>` captured via `tee -q -o <path>` is the metrics source (`num_cells`, `area`, `sequential_area`, `num_cells_by_type`); Yosys has **no working timing-report path** against a liberty-mapped netlist — timing summary is out of scope for this contract, deferred to Phase 4's OpenROAD/OpenSTA step; liberty is resolved via the already-shipped `find_pdk()` (`src/klayout_tools/pdk.py`), not a new PDK mechanism. |
| #397 → `openroad-invocation-survey.md` | OpenROAD `26Q3-771-g7cfb2105c9` | Batch invocation is `openroad -no_init -exit script.tcl`; wrap OpenROAD's native Tcl API stage-by-stage, not ORFS's Makefile; OpenROAD's own `-metrics <file>.json` channel is the strongest candidate for structured per-stage output but was **not confirmed end-to-end** (the survey's own run crashed mid-CTS under QEMU emulation, an environment limitation, not an OpenROAD defect); OpenROAD emits DEF, and the DEF→GDS merge is a `pya`/`klayout.db` operation this repo can run in-process (matching `klt drc`'s existing posture), not a `klayout -r`/`-rd` subprocess; wirelength/slack/utilization are available per-stage, not just at the end; P&R is seeded and stochastic. |
| #398 → `cocotb-verification-spike.md` | cocotb 2.0.1 + Icarus 13.0 / Verilator 5.050 | Invoke via the first-party `cocotb_tools.runner` Python API, never a generated Makefile; `results.xml`'s `<testcase>`/`<failure>`/`<skipped>` structure is the authoritative pass/fail source, not a raw subprocess exit code; the contract's pass/fail boundary must collapse to a single `status`/exit-code pair matching `klt eval`'s (PR #403) `valid`/`0`/`1`/`3` convention exactly; Verilator's `--coverage` + `verilator_coverage --write-info` produces a portable lcov `.info` file, Icarus has no coverage path. **This survey already drafted a complete request/response contract (its own §7)** — this document adopts it with only the cross-contract naming and #247-reconciliation changes noted in §3 below, rather than re-deriving it.

All three surveys ground their worked examples in the same forcing function,
marketing#56 (the GCD/RSA-modexp digital canary, operator ruling
2026-08-03), and the same design-space framing established in
[docs/design/digital-fleet-unit-abstraction-decision.md](digital-fleet-unit-abstraction-decision.md)
(#400): the three contracts below are composed **serially per candidate**
(RTL → synthesis → [functional verification] → place-and-route → GDS), never
fanned out across hosts *within* one candidate — parallelism, when it
exists, is **across candidates**, a decision this document does not
revisit.

## 1. Shared envelope conformance

All three contracts below emit through the same shared shape
[docs/json-contract.md](../json-contract.md) already defines for every `klt`
verb: a flat top-level payload plus `schema_version` (versioned per command,
starting at `1`); the `{"schema_version": 1, "error": {"command", "message"}}`
shape on stderr for a failed run, with **stdout left empty** and **no
envelope emitted** for that run (matching `klt extract`/`klt lvs`'s existing
"a failed run does not emit this envelope at all" convention); and the
`0`/`1`/`2` exit-code baseline (`0` success, `1` application error, `2`
usage error from argparse), with each contract stating below whether it
defines an additional code above `2` (only the functional-verification
contract does, matching PR #403's precedent exactly — see §4).

Each contract also emits the shared `provenance` block
(`klt_version`, `klayout_version`, `pdk`, `deck`) `docs/json-contract.md`
already defines once — `deck` names the liberty/LEF/cell-library asset set
resolved for the run (not a DRC/LVS rule deck, reusing the same `{name,
content_hash}` shape), and `pdk` is the resolved PDK triple
(`find_pdk()`'s own `{name, source, version}`), consistent with how `klt
drc`/`klt extract` already populate it.

## 2. Naming choices made once, applied to all three contracts

Two consistency decisions apply across all three contracts, made explicit
here rather than repeated per-contract:

- **`hdl_toplevel` is the top-level-module field name in all three
  contracts**, not `top`/`design_name`/`DESIGN_NAME` (the names the Yosys
  worked example's own `--top` flag, and OpenROAD-flow-scripts'
  `DESIGN_NAME` Makefile variable, use respectively). The
  cocotb-verification-spike.md contract (§398) already named this field
  `hdl_toplevel`; this document adopts that name for the synthesis and
  place-and-route contracts too, rather than inventing a second name for the
  same concept as it flows candidate-wide (RTL top module name in ->
  synthesized netlist's top module name -> placed/routed design's top module
  name — the same module, same name, at every stage of one candidate).
- **`engine` is present in every contract's request, even where only one
  engine is implemented today.** Synthesis's `engine` currently accepts only
  `"yosys"`, place-and-route's only `"openroad"` — but the field exists from
  day one (mirroring the functional-verification contract's already-decided
  `"icarus"`/`"verilator"` selector) so that a later Siemens backend (per
  Epic #391's own "Siemens" section) is an additive enum value and a new
  glue module, never a contract-shape change. This is the concrete
  mechanism behind the "engine-agnostic in shape" requirement — restated and
  checked per contract in §§4b/5b/6b below.

## 3. Reconciling field names against #247's proposed metric namespace

[#247](https://github.com/2AMLogic/klayout-tools/issues/247) ("adopt a
declared metric namespace... METRICS2.1-style names + aggregator/polarity
registry") is an **open, uncurated-for-approval proposal**
(`loom:operator-only` + `loom:curated`, not `loom:issue`) — a naming
convention to check the fields below against, not a landed spec these
contracts must implement. #247's own body names exactly three concrete
example names: `design__core__area`, `design__instance__count`, and
`timing__setup__ws` (worst slack), plus the general `domain__object__metric`
grammar and a `metric__...__corner:<name>` qualifier convention.

**Decision: none of the three contracts below adopt hierarchical
(`domain__object__metric`) names as their primary JSON field names.**
Reason: every already-shipped `klt` verb (`drc`'s `violation_count`/
`rule_counts`, `lvs`'s `mismatch_count`/`category_counts`, `extract`'s
`device_count`/`device_counts`, `sim`'s `passed`/`failed`/`corner_count`,
`layout-metrics`'s `cell_count`/`instance_count`) already uses flat,
`snake_case` field names, and #247 itself is not approved — adopting its
naming for three *new* contracts while every existing verb keeps flat names
would fragment `klt`'s own JSON surface into two conventions before either
convention has a working consumer. This is the explicit deviation the
curator's guidance asks for, not silence: **the crosswalk below is the
concrete "checked against #247" artifact**, so that if #247 is itself
promoted and adopted later, the rename map (old flat name → #247-style name)
already exists and does not need to be re-derived by whoever implements that
future migration.

| Contract field (this document) | #247-style crosswalk | Status |
| --- | --- | --- |
| synthesis `instance_count` | `design__instance__count` | **Exact match to #247's own stated example** — same concept (post-synthesis standard-cell instance count), different (flat) surface name today. |
| synthesis `area_um2` | `design__instance__area` | Natural extension of #247's grammar, **not** independently verified against LibreLane's own registry (unlike the two names #247 states verbatim) — flagged as this document's own inference, not a confirmed upstream name. |
| synthesis `sequential_area_um2` | *(no stated or inferable #247 analog)* | Deviation: a `klt`/Yosys-native breakdown of `area_um2` (sequential vs. combinational), not a concept #247's three examples cover. Kept as a free additive field per the Yosys survey's own recommendation (§2 of that survey). |
| synthesis `instance_counts_by_type` | *(no analog — map, not a scalar)* | Deviation: #247's namespace is a flat scalar-metric registry (one name → one number); a per-cell-type breakdown map has no place in that shape. Kept using `klt`'s own existing per-category-map convention (`rule_counts`/`device_counts`/`category_counts`). |
| P&R `core_area_um2` | `design__core__area` | **Exact match to #247's own stated example.** |
| P&R `die_area_um2` | *(no stated analog; natural sibling of `design__core__area`)* | Natural extension (`design__die__area`), not independently verified. |
| P&R `utilization_pct` | *(no stated analog)* | Natural extension following the grammar (`design__instance__utilization` is the plausible LibreLane-family name), not independently verified — this document does not assert it as a confirmed upstream name. |
| P&R `worst_slack_ns` | `timing__setup__ws` | **Exact match to #247's own stated example** (WNS = "worst slack"). |
| P&R `total_negative_slack_ns` | *(natural sibling, `timing__setup__tns`)* | Natural extension (TNS is the paired metric #247's own cited source, OpenROAD's `report_metrics.tcl`, computes alongside WNS — see the OpenROAD survey §1's `report_tns_metric`/`report_worst_slack_metric` pairing), not independently verified against #247's issue text itself. |
| P&R `wirelength_um` | *(no stated analog)* | Natural extension (`route__wirelength`), not independently verified. |
| P&R `setup_violation_count` / `hold_violation_count` | *(no stated analog; `route__drc_errors` is the nearest sibling grammar, for a different check)* | Deviation: #247's one routing-adjacent example (`route__drc_errors`) names DRC violations post-route, a different concept from setup/hold timing-check violation counts. No renaming performed; flagged as a related-but-distinct metric family for whoever eventually curates the full registry. |
| functional-verification `test_count`/`passed_count`/`failed_count`/`skipped_count`, `coverage.{line,toggle,branch,expr}_pct` | *(no analog)* | Deviation, reason: **different domain.** #247's registry (and its LibreLane/METRICS2.1 source) is a physical-design/timing/routing metric namespace; functional-verification pass/fail and structural coverage percentages are not physical-design metrics at all — there is no METRICS2.1 analog to check against, not a naming clash. |

**What this reconciliation is not**: it is not a proposal to expand #247's
own scope, and it does not create a `metrics` block or any other new
JSON-emitting mechanism in this repo. It is the field-by-field check
Acceptance Criteria #2 requires, performed once here so Phase 2/4's
implementation issues do not have to re-derive it, and so a future #247
promotion has a ready crosswalk rather than starting from zero.

## 4. Contract: synthesis (`klt synthesize`)

```
klt synthesize <request.json> [--format text|json]
```

Takes a request document — like `klt lvs`/`klt eval`/the functional-
verification contract below — since RTL sources plus PDK/liberty selection
plus optional constraints is richer than a flag line carries cleanly.

### Request

```json
{
  "schema": "klt.synthesize.request/1",
  "engine": "yosys",
  "sources": ["gcd.v"],
  "hdl_toplevel": "gcd",
  "pdk": {
    "cell_library": "sky130_fd_sc_hd",
    "corner": "tt_025C_1v80"
  },
  "constraints": { "clock_period_ns": null }
}
```

| Field | Type | Description |
| --- | --- | --- |
| `schema` | string | Request contract identifier + major version (matches `klt lvs`/`klt gen`/`klt sim`/the functional-verification contract's request-side convention). |
| `engine` | string | `"yosys"` (only value implemented; present from day one per §2). |
| `sources` | array\<string\> | RTL source file paths (`read_verilog` inputs, per the Yosys survey §1). |
| `hdl_toplevel` | string | The design's top module name (§2). |
| `pdk.cell_library` | string | Standard-cell library name (`sky130_fd_sc_hd` today) — resolved to a liberty file via the already-shipped `find_pdk()`/`libs_ref` discovery (Yosys survey §0), not a new PDK-fetch mechanism. |
| `pdk.corner` | string | Liberty corner selector (e.g. `tt_025C_1v80`); defaults to the nominal corner `klt pdk`'s own nominal-corner selection already picks when omitted. |
| `constraints.clock_period_ns` | number \| null | *(Superseded by issue #807: now **consumed**, as ABC's own `abc -D <picoseconds>` delay target — see `docs/cli/synthesize.md`. The finding below is still accurate about Yosys itself; the shipped command reaches the engine's delay knob without an SDC.)* **Carried through, not consumed by Yosys itself** — per the Yosys survey §1's explicit finding that this invocation surface has no SDC-reading step. Echoed in the response's `provenance`/request-echo fields so it reaches Phase 4's P&R/STA step unmodified; `null` (the default) means "no target period stated at this stage." |

### Response

```json
{
  "schema_version": 1,
  "engine": "yosys",
  "engine_version": "0.67+post",
  "hdl_toplevel": "gcd",
  "status": "ok",
  "instance_count": 335,
  "area_um2": 2951.5808,
  "sequential_area_um2": 1251.2,
  "instance_counts_by_type": {
    "sky130_fd_sc_hd__a211o_1": 1,
    "sky130_fd_sc_hd__dfrtp_1": 50
  },
  "timing": null,
  "netlist_path": ".klt/synthesize/gcd_synth.v",
  "script_path": ".klt/synthesize/synth_gcd.ys",
  "provenance": {
    "klt_version": "0.4.2",
    "klayout_version": null,
    "pdk": { "name": "sky130A", "source": "volare", "version": "<stamp>" },
    "deck": { "name": "sky130_fd_sc_hd__tt_025C_1v80", "content_hash": "sha256:<hex>" },
    "input": { "content_hash": "sha256:<hex>" }
  }
}
```

##### Top-level fields

| Field | Type | Description |
| --- | --- | --- |
| `schema_version` | integer | Per-command version, per `docs/json-contract.md`. |
| `engine` / `engine_version` | string | Echo of the request's engine, plus the resolved Yosys build string (`yosys -V`-equivalent). |
| `hdl_toplevel` | string | Echo of the request. |
| `status` | string | `"ok"` — synthesis has no pass/fail concept of its own (see "Exit codes" below); a failed run never emits this envelope. |
| `instance_count` | integer | `stat -json`'s `num_cells` (Yosys survey §2/§4) — total standard-cell instances after liberty mapping. **Deliberately not named `cell_count`**: `klt layout-metrics`'s existing `cell_count` field counts *distinct cell definitions* in a GDS hierarchy (from `klt cells`), a different concept from a post-synthesis instance tally. Reusing `cell_count` here would silently collide two different meanings under one name across two `klt` verbs — `instance_count` matches `layout-metrics`'s own `instance_count` field's semantics instead (`layout-metrics`'s "sum of every cell's `instances`"), which is the correct precedent to reuse. |
| `area_um2` | number | `stat -json`'s `area`, in µm² (the liberty's own unit, Yosys survey §2/§4). |
| `sequential_area_um2` | number | `stat -json`'s `sequential_area` — a free additive field per the Yosys survey's own recommendation, useful as a P&R floorplan hint. |
| `instance_counts_by_type` | object\<string, int\> | `stat -json`'s `num_cells_by_type`, keys sorted for determinism — the synthesis analogue of `klt drc`'s `rule_counts` / `klt extract`'s `device_counts`. |
| `timing` | object \| null | *(Superseded by issue #807: the reserved field is now populated with ABC's own `stime -p` critical path, as a self-labelling `{source, wire_load, critical_path_ps, delay_target_ps}` object — a pre-layout, wire-free estimate, not signoff STA, which remains Phase 4's. Additive, exactly as this row reserved for.)* **Always `null` in this contract as scoped.** Per the Yosys survey §3.5/§3.6: Yosys's own `sta`/`ltp` passes could not produce a usable timing report against a liberty-mapped netlist in that survey. Rather than commit to a Yosys-native timing field this spike cannot back with a working recipe, the field is reserved (present, typed, always `null` today) and deferred to Phase 4's OpenROAD/OpenSTA step, which already must run STA for P&R signoff. |
| `netlist_path` | string | The mapped gate-level netlist (`write_verilog -noattr`'s output) — a referenced artifact, matching `klt extract`'s `netlist_path` convention. Never re-derive `instance_count`/`area_um2` by parsing this file (Yosys survey §2's explicit warning). |
| `script_path` | string | The generated `.ys` script — kept as a debuggable, saveable artifact (Yosys survey §1), the same "generated deck is kept, not deleted" discipline `klt sim`'s corner decks already follow. |
| `provenance` | object | Shared block (§1). `deck` names the resolved liberty file; `pdk` is `find_pdk()`'s resolved triple. |

### Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Synthesis succeeded, netlist written. |
| `1` | Failed to run — bad request, unreadable RTL source, elaboration/hierarchy error, unresolvable `pdk.cell_library`/`corner` (no matching liberty via `find_pdk()`), or a Yosys/ABC engine error. |
| `2` | Usage error (missing argument, bad `--format` value) — from argparse. |

**No exit code `3`.** Matching `klt extract`'s reasoning exactly (there is no
"ran but found problems" outcome for synthesis): it either produces a
netlist or it fails. Synthesis's numeric outputs (`instance_count`,
`area_um2`) are not a pass/fail gate themselves — a caller wanting a
threshold on them composes this contract into `klt eval`'s descriptor
(`layout-metrics`'s `cell_count` threshold in `docs/cli/eval.md`'s own
example is the precedent), rather than this contract inventing its own
threshold/pass-fail concept.

### Build/wrap decision

Scored against `docs/ARCHITECTURE.md`'s three-criteria rewrite rule, the
same way the cocotb spike (§398 §9) scored its own engine choice:

1. **Bottleneck or ceiling — fails.** No `klt`-native synthesis capability
   exists today; nothing to be a ceiling of.
2. **Oracle exists — holds.** A synthesized netlist can be cross-checked
   against the original RTL's behavior by running *both* through the
   functional-verification contract below with the same testbench — the
   synthesized netlist is `sources` to that contract exactly as the RTL is,
   a working differential oracle with no extra tooling. `stat -liberty`'s
   area/cell-count arithmetic is also the engine's own, not `klt`-derived.
3. **Unlock — fails today.** Nothing here is structurally impossible through
   Yosys + ABC; a from-scratch synthesis engine (technology mapping,
   logic optimization) is a multi-year EDA undertaking with no plausible
   unlock this epic's forcing function (marketing#56) needs.

**Verdict: wrap Yosys + bundled ABC, exactly as named** (both fully
permissive — ISC / UC Berkeley academic license, Yosys survey §3.1 —
compatible with `klt`'s MIT posture, no copyleft anywhere in the wrapped
stack). What `klt` builds and owns: the request/response marshalling, `.ys`
script generation (Yosys survey §1's exact pass sequence), the
`tee -q -o <path>` capture-and-parse of `stat -liberty ... -json`, and the
`find_pdk()`-based liberty resolution glue. The wrapped dependency itself
(Yosys's `synth`/`dfflibmap`/`abc -liberty` passes) is never modified or
reimplemented.

### Engine-agnostic check (4b)

If Yosys were replaced by a different synthesis engine (a Siemens tool, or a
different open-source alternative), only two things change: the `engine`
enum gains a value, and a new glue module generates that engine's own input
deck and parses its own metrics output into this contract's response shape.
None of `sources`/`hdl_toplevel`/`pdk`/`constraints` on the request side, or
`instance_count`/`area_um2`/`instance_counts_by_type`/`netlist_path` on the
response side, embed Yosys-specific vocabulary — no field is named after a
Yosys pass (`stat`, `dfflibmap`, `abc`) or shaped like Yosys's own JSON
(`write_json`'s per-instance connectivity graph, which this contract
deliberately does not surface — see `netlist_path` above). Checked directly
by re-reading every field name above against the Yosys survey's own §1
pass-name list and §2's three output-surface names: no match.

## 5. Contract: place-and-route (`klt place-and-route`)

```
klt place-and-route <request.json> [--format text|json]
```

### Request

```json
{
  "schema": "klt.place_and_route.request/1",
  "engine": "openroad",
  "netlist": "gcd_synth.v",
  "hdl_toplevel": "gcd",
  "pdk": {
    "cell_library": "sky130_fd_sc_hd",
    "corner": "tt_025C_1v80"
  },
  "floorplan": {
    "method": "utilization",
    "utilization_pct": 38,
    "aspect_ratio": 1.0,
    "core_margin_um": 2.0,
    "site": "unithd"
  },
  "io": { "layer_h": "met3", "layer_v": "met2" },
  "constraints": { "clock_port": "clk", "clock_period_ns": 1.1 },
  "seed": 1,
  "target_stage": "route"
}
```

| Field | Type | Description |
| --- | --- | --- |
| `schema` | string | Request contract identifier + major version. |
| `engine` | string | `"openroad"` (only value implemented; present from day one per §2). |
| `netlist` | string | The gate-level netlist path — the synthesis contract's own `netlist_path` output, one candidate's serial hand-off (§0). |
| `hdl_toplevel` | string | Top module name (§2) — the same value the synthesis request/response carried. |
| `pdk.cell_library` / `pdk.corner` | string | Same shape as the synthesis contract's `pdk` block; resolves LEF + liberty for this run. **Open gap, named in the OpenROAD survey §5, not resolved here**: `find_pdk()` does not yet resolve LEF files (`_ASSET_LAYOUT` has no `lef` key) — Phase 4 needs a LEF resolver alongside the already-working liberty resolution; this contract's shape does not change either way. |
| `floorplan.method` | string | `"utilization"` (default, used by the worked example) or `"explicit"` (die/core rectangle) or `"def"` (re-use an existing DEF) — the three non-padframe methods the OpenROAD survey §2 documents (the fourth, IO-ring/footprint, is out of scope for a core-only block per that survey and not modeled here). |
| `floorplan.utilization_pct` / `.aspect_ratio` / `.core_margin_um` / `.site` | number / number / number / string | `initialize_floorplan -utilization/-aspect_ratio/-core_space/-site`'s own parameters (OpenROAD survey §2), present when `method: "utilization"`. |
| `io.layer_h` / `.layer_v` | string | Horizontal/vertical I/O routing layers for `place_pins` (OpenROAD survey §2). |
| `constraints.clock_port` / `.clock_period_ns` | string / number | Minimal SDC-equivalent input OpenROAD's STA passes actually consume (unlike Yosys — see §4's `constraints` field, which this contract's constraints are the eventual consumer of). |
| `seed` | integer | Placement/routing seed, echoed in the response. **Required by design, not optional** — per [digital-fleet-unit-abstraction-decision.md](digital-fleet-unit-abstraction-decision.md)'s own finding that P&R is genuinely stochastic (OpenROAD survey §6, point 3) and a stored result must be reproducible, the same "reproducibility provenance" role `environment.remote` fields already play for `klt sim`. |
| `target_stage` | string | One of `"floorplan"`, `"place"`, `"cts"`, `"route"` (default) — how far this run is asked to go. See "Partial-completion design" below; this is the mechanism that answers the OpenROAD survey's own flagged concern about field completeness when routing/CTS does not finish. |

### Response

```json
{
  "schema_version": 1,
  "engine": "openroad",
  "engine_version": "26Q3-771-g7cfb2105c9",
  "hdl_toplevel": "gcd",
  "status": "ok",
  "stage_reached": "route",
  "seed": 1,
  "die_area_um2": 6404.0,
  "core_area_um2": 5885.645,
  "utilization_pct": 69.0,
  "wirelength_um": 5625.9,
  "worst_slack_ns": -1.55,
  "total_negative_slack_ns": -68.37,
  "fmax_mhz": 404.60,
  "setup_violation_count": 69,
  "hold_violation_count": 0,
  "estimated_power_mw": 6.01,
  "stages": [
    { "name": "floorplan", "utilization_pct": 39.3, "worst_slack_ns": -1.15, "total_negative_slack_ns": -48.24 },
    { "name": "global_placement", "utilization_pct": 73.5, "wirelength_um": 6361.6, "worst_slack_ns": -1.48, "total_negative_slack_ns": -67.30, "fmax_mhz": 406.41 },
    { "name": "detailed_placement", "utilization_pct": 69.0, "wirelength_um": 5625.9, "worst_slack_ns": -1.55, "total_negative_slack_ns": -68.37, "fmax_mhz": 404.60 }
  ],
  "def_path": ".klt/place-and-route/6_final.def",
  "gds_path": ".klt/place-and-route/gcd.gds",
  "provenance": {
    "klt_version": "0.4.2",
    "klayout_version": "0.30.10",
    "pdk": { "name": "sky130A", "source": "volare", "version": "<stamp>" },
    "deck": { "name": "sky130hd", "content_hash": "sha256:<hex>" },
    "input": { "content_hash": "sha256:<hex>" }
  }
}
```

##### Top-level fields

| Field | Type | Description |
| --- | --- | --- |
| `status` | string | `"ok"` — like synthesis, place-and-route has no pass/fail concept of its own; see "Exit codes." |
| `stage_reached` | string | The last stage this run actually completed — `"floorplan"` / `"place"` / `"cts"` / `"route"`. See "Partial-completion design" below. |
| `seed` | integer | Echo of the request's seed. |
| `die_area_um2` / `core_area_um2` | number | From `initialize_floorplan`'s own report (OpenROAD survey §3, `IFP-0102`/`IFP-0104`). |
| `utilization_pct` | number | Effective utilization at `stage_reached` (OpenROAD survey §3's `detailed place report_design_area`, for a full run). |
| `wirelength_um` | number | HPWL at `stage_reached` (OpenROAD survey §3's detailed-placement legalized HPWL for a full run). |
| `worst_slack_ns` / `total_negative_slack_ns` | number | WNS/TNS at `stage_reached`, from OpenROAD's `report_worst_slack`/`report_tns` (OpenROAD survey §1/§3). Negative values are expected and not an error — see the OpenROAD survey §3's own note that the stock `gcd` example's aggressive clock constraint produces persistent negative slack by design. |
| `fmax_mhz` | number \| null | From `report_fmax_metric`, when timing analysis ran at `stage_reached`; `null` before placement (floorplan-stage ideal-clock STA does not report an `fmax`, per the survey's own per-stage table). |
| `setup_violation_count` / `hold_violation_count` | integer | From `report_check_types` (OpenROAD survey §3). |
| `estimated_power_mw` | number \| null | Populated once placement has run (OpenROAD survey §3); `null` before that stage. |
| `stages` | array\<object\> | One entry per completed stage through `stage_reached`, each with whatever subset of the top-level metric fields that stage's own OpenROAD reports populate (per the OpenROAD survey §6 finding #2: "wirelength, slack, and utilization are all available per-stage, not just at the end" — discarding that would throw away information the engine already surfaces cheaply). The top-level fields above are always the **last** entry in `stages` restated at top level, for a caller that only wants the final number without indexing the array. |
| `def_path` / `gds_path` | string \| null | Referenced artifacts (matching `netlist_path`'s "artifacts are paths" discipline). `def_path` is populated once `write_def` has run (`stage_reached` is `"route"` or later within that stage); `gds_path` is populated only once the DEF→GDS merge (OpenROAD survey §4) has also completed — both `null` when `stage_reached` stops short of that. |
| `provenance` | object | Shared block (§1); `deck` names the resolved LEF/liberty/GDS-view platform set (e.g. `sky130hd`). |

### Partial-completion design (resolving the OpenROAD survey's flagged risk)

The Implementation Guidance for this issue flags that CTS/routing did not
complete in the OpenROAD survey's own sandboxed run (an emulation artifact,
not an OpenROAD defect — see that survey's "Environment limitation"
section) and asks this document to state explicitly how the contract
handles a run that does not reach full routing.

**Resolution: `target_stage` makes "how far to go" an explicit request
input, not an ambient possibility a response has to apologize for.** A
request with `target_stage: "place"` asks only for floorplan through
detailed placement — a successful (`exit 0`) run of that request has
`stage_reached: "place"`, `def_path`/`gds_path` both `null` by design (never
requested), and every metric field populated through placement, exactly the
real data the OpenROAD survey's own worked example captured (§3's table:
synthesis through detailed placement, all real `[RUN]` numbers). This is a
**normal, successful, partial-by-request outcome** — not a degraded one.

A request with `target_stage: "route"` (the default — the common case, a
full RTL-to-GDS candidate) that the engine fails to complete due to an
internal OpenROAD/CTS/routing error is a **failed run** (exit `1`, no
envelope) — the requested deliverable (a routed, GDS-emitting result) was
not produced, and this contract does not invent a third "sort of succeeded"
state for that case, matching `klt lvs`'s own "no such third success state"
reasoning (cited directly in the cocotb spike §7). The distinction is
clean: `stage_reached` in a **successful** response always equals (or
exceeds, never falls short of) the request's own `target_stage` — a
response where those differ never gets emitted.

### Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Place-and-route reached (at least) the requested `target_stage`; the documented payload is on stdout. |
| `1` | Failed to run — bad request, unresolvable netlist/PDK/LEF, a floorplan spec with more than one method set (mirroring ORFS's own `methods_defined > 1` check, OpenROAD survey §2), or an OpenROAD engine error that stops the run before reaching the requested `target_stage`. |
| `2` | Usage error (missing argument, bad `--format` value) — from argparse. |

**No exit code `3`.** Timing slack and violation counts are **data**, not a
built-in pass/fail gate — a negative `worst_slack_ns` is an expected,
correctly-reported number (the OpenROAD survey's own stock `gcd` example is
persistently negative by its constraint file's own design, §3), not a
contract-level failure. A caller wanting "did timing close" as a pass/fail
gate composes this contract into `klt eval`'s descriptor with an explicit
`threshold` (e.g. `{"metric": "worst_slack_ns", "min": 0}`), the same
mechanism `docs/cli/eval.md`'s own example already uses for
`layout-metrics`'s `cell_count` — this contract does not duplicate that
mechanism internally.

### Build/wrap decision

1. **Bottleneck or ceiling — fails.** No `klt`-native place-and-route
   capability exists today.
2. **Oracle exists — holds, and doubly so.** Placed-and-routed GDS output
   can be checked with `klt drc`/`klt precheck`/`klt lvs` against the same
   sky130 decks this repo already exercises on hand-drawn layout (Epic
   #391's own Success Criteria names this explicitly) — a real, already-
   built oracle, not a hypothetical one. Independently, every numeric field
   in §5's response is the engine's own computed report data (WNS/TNS,
   HPWL, utilization), never re-derived by `klt` from a lower-level
   artifact.
3. **Unlock — fails today.** Nesterov-based global placement (`gpl`) and
   FastRoute/TritonRoute represent years of specialized placement/routing
   algorithm research; no rewrite unlock is credible at this repo's scope
   or forcing function.

**Verdict: wrap OpenROAD's native Tcl API directly, stage by stage — not
OpenROAD-flow-scripts' Makefile.** This is the same "wrap the engine, not
the orchestrator" conclusion the siliconcompiler survey reached for
`siliconcompiler`'s own `Flowgraph`/`Project` monolith, and the OpenROAD
survey (§1/§5) independently reaches it again for ORFS's ~800-line
Makefile: take ORFS's platform `config.mk` as **reference data** (which
LEF/lib files, which cells to exclude, which layers), never as
infrastructure to depend on. What `klt` builds and owns: per-stage Tcl
script generation (or env-var injection — the OpenROAD survey §1 leaves
this as a small, self-contained implementation choice, not resolved here),
`-metrics <file>.json` capture **as the primary structured-output channel,
to be confirmed end-to-end in Phase 4** (the OpenROAD survey's own run did
not reach a point where this could be verified live — flagged as an open
risk carried forward, not silently assumed), and the DEF→GDS merge logic
ported directly onto `klayout.db` in-process (per `def2stream.py`'s own
plain-`pya`-function structure, OpenROAD survey §4) — never the `klayout.sh
-zz -rd ... -r def2stream.py` subprocess pattern ORFS itself uses.

### Engine-agnostic check (5b)

If OpenROAD were replaced by a different P&R engine (a Siemens tool, or a
different open-source alternative), only the `engine` enum and a new
Tcl/API-generation-and-metrics-parsing glue module change. No field in
§5's request or response is named after an OpenROAD Tcl proc
(`report_worst_slack_metric`, `clock_tree_synthesis`) or an ORFS env var
(`CORE_UTILIZATION`, `DIE_AREA`) verbatim — `floorplan.utilization_pct` is a
`klt`-chosen name deliberately distinct from ORFS's own `CORE_UTILIZATION`,
checked directly against the OpenROAD survey §1/§2's own command/env-var
tables for exact-string collisions: none found.

## 6. Contract: functional verification (`klt functional-verification`)

```
klt functional-verification <request.json> [--format text|json]
```

**This contract is adopted from `docs/design/cocotb-verification-spike.md`
§7 (#398) essentially as proposed** — that survey already drafted a complete
request/response shape and explicitly built it to satisfy PR #403's
`valid`/exit-code convention (its own §8, cited by number). This section
restates it for completeness alongside the other two contracts and performs
the two checks this issue's own Implementation Guidance asks for that the
source survey did not perform: the #247 reconciliation (done above in §3)
and an explicit engine-agnostic-shape check (§6b below). No field is
renamed from the source survey's own proposal.

### Request

```json
{
  "schema": "klt.functional_verification.request/1",
  "engine": "icarus",
  "sources": ["gcd.v"],
  "hdl_toplevel": "gcd",
  "testbench": { "module": "test_gcd", "testcase": null },
  "options": {
    "coverage": false,
    "timescale": ["1ns", "1ps"]
  }
}
```

(Field table: see cocotb-verification-spike.md §7 — unchanged here.)
`sources` is intentionally the same shape as the synthesis contract's own
`sources` field, and can point at the original RTL *or* the synthesis
contract's `netlist_path` output — a gate-level equivalence check against
the same testbench needs no contract change, just a different `sources`
value, which is the concrete form of the oracle §9 of that survey
describes.

### Response

(Full shape: see cocotb-verification-spike.md §7 — unchanged here.)
Top-level fields of note for cross-contract consistency: `hdl_toplevel`
matches §2's naming decision (the source survey already used this name,
which is why §2 adopted it for the other two contracts rather than the
reverse); `status` is `"pass"`/`"fail"` (never a third state, per that
survey's own §8); `coverage` is `null` unless `options.coverage: true` and
`engine: "verilator"`.

### Exit codes

| Code | Meaning |
| --- | --- |
| `0` | All tests passed (`status: "pass"`). |
| `1` | Failed to run — bad request, unparseable RTL sources, build/elaboration error, simulator crash, no `results.xml` produced. |
| `2` | Usage error — from argparse. |
| `3` | Ran successfully; at least one test failed (`status: "fail"`). |

This is the **one contract of the three that does define an exit `3`**,
deliberately: unlike synthesis and place-and-route (which report metrics
with no pass/fail concept of their own, per §4/§5's "Exit codes" sections),
functional verification's entire purpose is a pass/fail verdict — per Epic
#391's own body, this is "the hard gate in #387's `valid` field." `status:
"pass"` → exit `0` → `valid: true` at the `klt eval` boundary; `status:
"fail"` → exit `3` → `valid: false` — exactly PR #403's own convention,
with no adaptation needed (cocotb spike §8).

### Build/wrap decision

Scored in full in cocotb-verification-spike.md §9: bottleneck/ceiling fails
(no existing capability), oracle exists and holds trivially (a testbench
is inherently cross-checked against a reference the moment it is written;
running the same testbench against both Icarus and Verilator is itself a
working differential oracle for the contract's own result-parsing), unlock
fails today. **Verdict (unchanged from that survey): wrap cocotb +
Verilator + Icarus**, invoked through the Python `Runner` API, never a
generated Makefile.

### Engine-agnostic check (6b)

This is the one contract among the three with **empirical, not just
structural, confirmation** of engine-agnosticism: the source survey ran the
*identical* `test_gcd.py` testbench against both `engine: "icarus"` and
`engine: "verilator"` and got byte-for-byte-equivalent pass/fail structure
(`TESTS=3 PASS=2 FAIL=1 SKIP=0`, same assertion text — cocotb spike §6,
"Verilator backend" paragraph) — the contract's `tests[]`/`status`/
`coverage` shape did not need to change between two structurally different
simulators (a compiler vs. an interpreter). If a third simulator (or a
commercial one, engine posture permitting) were added, only the `engine`
enum grows and a new `Runner`-configuration glue module is added — no field
in the request or response is named after cocotb-, Icarus-, or Verilator-
specific vocabulary (checked directly: no field is named after a cocotb
internal, e.g. `regression_manager`, or a simulator-specific artifact name
beyond the generic, already-referenced `results.xml`/`coverage.info`
paths).

## 7. Composition: how the three contracts chain into one candidate

Per [digital-fleet-unit-abstraction-decision.md](digital-fleet-unit-abstraction-decision.md),
one "candidate evaluation" is one full pass through this pipeline, composed
**serially**, never fanned out across the three contracts within one
candidate:

```
RTL sources ──┬──► klt synthesize ──► netlist_path ──┬──► klt place-and-route ──► def_path / gds_path
              │                                       │
              └──► klt functional-verification ◄──────┘
                   (sources: RTL, or netlist_path for a
                   gate-level equivalence re-check)
```

- **Synthesis → place-and-route**: the synthesis response's `netlist_path`
  is the P&R request's `netlist` field — the same file, no re-derivation.
- **RTL/netlist → functional verification**: the verification contract's
  `sources` field is generic enough to take either the original RTL or the
  synthesized netlist unmodified — no contract change is needed to support
  running the same testbench both pre- and post-synthesis (a gate-level
  equivalence check), which directly answers the synthesis contract's own
  build/wrap oracle argument in §4.
- **`hdl_toplevel` is the one identifier that must stay identical across
  all three requests for one candidate** — this is why §2 fixed that name
  first, before any per-contract field was drafted.
- Nothing in this composition requires a change to `remote_fleet.py` /
  `remote_launcher.py` / `remote_transport.py` — per
  digital-fleet-unit-abstraction-decision.md, a fleet job for one candidate
  is a caller-supplied `ShardRunner` that invokes these three contracts in
  sequence inside one host, unrelated to this document's own scope.

## 8. Wrap/build decision summary

| Contract | Engine(s) | Bottleneck/ceiling | Oracle | Unlock | Verdict |
| --- | --- | --- | --- | --- | --- |
| synthesis | Yosys + ABC | Fails | Holds (cross-check via functional-verification contract; liberty-derived arithmetic is the engine's own) | Fails today | Wrap, unmodified |
| place-and-route | OpenROAD | Fails | Holds, doubly (existing `klt drc`/`lvs`/`precheck` decks + engine's own report arithmetic) | Fails today | Wrap OpenROAD's Tcl API directly, not ORFS's Makefile |
| functional verification | cocotb + Icarus/Verilator | Fails | Holds trivially (testbench is its own oracle; cross-engine run is a second, empirical oracle — confirmed live in #398) | Fails today | Wrap, via the Python `Runner` API, never a generated Makefile |

All three verdicts are "wrap," consistent with Epic #391's own "Siemens"
framing: because each contract's shape is checked engine-agnostic (§§4b/5b/
6b) and the wrap decision touches only glue code, a later commercial backend
is an additive `engine` value and a new glue module for whichever contract
it targets — never a rewrite of any of the three contracts documented here.

## Out of scope for this spike

No `klt` subcommand, dependency, or code under `src/klayout_tools/` was
added or changed. No new tool was invoked — every number cited above is
carried from the three merged surveys' own worked examples, not
re-measured. Phase 2 (synthesis verb), Phase 3 (verification verb), and
Phase 4 (P&R verb) of Epic #391 carry the implementation, gated on this
document per that epic's own Success Criteria and this issue's own
Acceptance Criteria #5 (human/Champion review before any Phase 2+ issue is
created).

## Open questions carried forward for Phase 2–4 (not resolved here)

- **Synthesis (#396 → Phase 2)**: what `klt synthesize` should do when
  `find_pdk()` resolves an install with no `sky130_fd_sc_hd` liberty at
  all (Yosys survey §0); pinning a reproducible Yosys version/commit for CI
  (Yosys survey §3.4 — Ubuntu 24.04's `apt` package is ~30 releases stale).
- **Place-and-route (#397 → Phase 4)**: a `klt pdk`-level LEF resolver
  (currently only `.lib` is resolved, OpenROAD survey §5); confirming
  `-metrics <file>.json` actually captures every `*_metric` Tcl proc's value
  end-to-end for a full stage (OpenROAD survey §6, not confirmed live due to
  the sandbox's CTS crash); the env-var-injection vs. generated-Tcl-script
  choice for parameterizing OpenROAD stages (OpenROAD survey §1, left open
  deliberately).
- **Functional verification (#398 → Phase 3)**: testbench provenance (who
  authors `testbench.module` — hand-written vs. generator-assisted);
  `random_seed` reproducibility (cocotb's `Runner` API does not surface this
  as a first-class request input today); cocotb 2.0.1's Python ≤3.13 ceiling
  needs an explicit decision once Phase 3 pins dependency bounds; CI
  provisioning for cocotb/Verilator/Icarus (no pinned install path exists
  yet).
- **#247's own promotion**: if #247 is later curated and approved, the
  crosswalk table in §3 is the starting rename map — this document does not
  pre-build a `metrics` block or registry mechanism for that eventuality,
  per the "don't design speculative contract surface" discipline the LVS
  spike's parasitics addendum already established for this repo.

## Related

- #391 parent epic
- #396 Yosys invocation survey → [yosys-synthesis-spike.md](yosys-synthesis-spike.md)
- #397 OpenROAD invocation survey → [openroad-invocation-survey.md](openroad-invocation-survey.md)
- #398 cocotb/Verilator/Icarus survey → [cocotb-verification-spike.md](cocotb-verification-spike.md)
- #400 fleet-scheduler unit-abstraction decision → [digital-fleet-unit-abstraction-decision.md](digital-fleet-unit-abstraction-decision.md)
- #247 proposed metric namespace (reconciled in §3, not adopted as primary field names)
- #387 single scored gate / #403 `klt eval` (the `valid`/exit-code convention the functional-verification contract matches exactly)
- marketing#56 — GCD/RSA-modexp canary, the common forcing function behind all three source surveys
