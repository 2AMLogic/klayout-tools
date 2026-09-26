# `klt characterize`

SPICE-driven NLDM timing characterization of **one** standard cell at **one**
PVT corner, emitting a Liberty (`.lib`) model (issue #2502).

```
klt characterize <request> [-o OUTDIR] [--lib PATH] [--backend BACKEND]
                          [--keep-artifacts] [--format text|json]
```

## Why this exists

`klt` could already *consume* Liberty from three verbs — `klt synthesize`,
`klt place-and-route`, and `klt sta` all resolve a `.lib` through
`pdk_cells.resolve_liberty_for_cell_library` — and `native/statime` could
*parse* one (`liberty.rs` + `nldm.rs`'s bilinear interpolation). Nothing in
the tree **produced** one. Every other stage of the cell flow had an open
answer (layout, DRC, LVS, extraction, simulation, abstraction, timing,
power); characterization did not, so a cell you drew yourself could be
DRC/LVS-clean and simulation-verified and still be invisible to the timing
tools that consume libraries.

`klt characterize` closes that gap for the case that matters first: a
combinational cell, one corner, delay and transition time. Hand-rolling it
is a day of scripting and a large surface for silent error — the side-input
state a NAND's `A -> Y` arc must be measured under, the ramp duration that
yields a requested *threshold-to-threshold* input slew, the mapping from
input edge to `cell_rise` vs. `cell_fall`, whether the transient window was
long enough for the output to settle before the next edge. Each of those is
a way to produce a plausible table that is wrong.

Like `klt sim`/`klt sta`, this command takes a **request document**, not
positional file args. Relative paths inside it (`cell.netlist`,
`models.lib`, `options.osdi_preload[]`, `output.*`) resolve against the
**request file's own directory**.

- `<request>` — path to a request JSON file.
- `-o`/`--outdir` — where the generated testbench, generated `klt sim`
  request, raw sim report, round-trip probe, and (by default) the emitted
  `.lib` are written. Default: a `.klt/characterize/` directory next to the
  request file, or the request's own `output.outdir`.
- `--lib` — override the emitted Liberty path (default: the request's own
  `output.lib`, else `<outdir>/<library name>.lib`).
- `--backend` — `klt sim` execution backend for the sweep. The grid is a
  **single corner**, so the default `local` is normally correct.
- `--keep-artifacts` — keep the per-corner ngspice log/deck under
  `<outdir>/sim/`.
- `--format` — `text` (default) or `json`.

Requires `ngspice` on `$PATH` (it is `klt sim` that runs it — see
[`sim.md`](sim.md)). Needs no KLayout, no Docker, and no PDK unless the
request's `models` block names one.

## Scope of this command

**In scope**: one cell; one PVT corner; every *combinational* timing arc of
that cell; `cell_rise`, `cell_fall`, `rise_transition`, `fall_transition`,
`related_pin`, `timing_sense`, `timing_type : combinational`.

**Out of scope**, deliberately — read the absence of a group as "this
command did not measure it", never as an assertion about the cell:

| Not emitted | Where it belongs |
|---|---|
| `rise_power` / `fall_power` / `leakage_power` | issue #2503 |
| more than one cell per invocation (batch mode) | issue #2503 |
| vendor-`.lib` arc-by-arc accuracy validation | issue #2503 |
| more than one corner per invocation / per file | invoke once per corner; see "Multiple corners" below |
| sequential-cell constraint arcs (`setup_*`/`hold_*`/`recovery_*`), `ff`/`latch` groups | not yet filed — a sequential cell is **refused**, not mis-characterized (see "Sequential cells are refused") |
| measured pin capacitance | issue #2512 — `pins[].capacitance_pf` is echoed from the request when given and the attribute is omitted when it is not |
| `when`-qualified arc splitting, `bus`/`bundle` pins, `internal_power` | not yet filed |

## How it works

1. **Arcs are derived, not declared.** Each output pin's Liberty `function`
   is parsed and evaluated over the input cube. An arc exists for every
   (output, input) pair where toggling the input can toggle the output, and
   each arc gets a **sensitizing vector**: the constant values the *other*
   inputs are held at so the related input actually propagates. For
   `function : "!(A*B)"` that yields two arcs, `A -> Y` measured with `B`
   high and `B -> Y` measured with `A` high — not the all-low state where a
   NAND output never moves. Enumeration is exhaustive over `2**n` (standard
   cells have a handful of inputs) and deterministic (first sensitizing
   vector in a fixed order wins), so the same metadata always produces the
   same arcs and the same stimulus. See
   `src/klayout_tools/characterize_arcs.py`.
2. **One deck, one simulator run.** The generated testbench instantiates the
   cell once per (arc, input transition, output load) triple — each instance
   with a private `PWL` ramp source, a private output node, and a private
   load capacitor, all sharing one supply — and a single `tran` analysis
   drives every instance's related input low → high → low. One instance
   therefore yields **both** output edges, hence all four tables for its grid
   point. A 7x7 grid of a 2-input cell is 98 instances in one deck, which is
   one `klt sim` corner, not 98 simulator invocations. (That is also why this
   verb is safe on a shared host: there is no hand-rolled `ngspice` grid to
   fan out.)
3. **Four `.meas` cards per grid point.** Delay is measured
   input-threshold → output-threshold; transition is measured between the
   library's own slew thresholds on the output edge. Which *input* edge
   produces the rising output edge follows the arc's measured polarity, so a
   `negative_unate` arc's `cell_rise` is correctly triggered by the input's
   *falling* edge.
4. **Emit and verify.** Tables are reshaped into a Liberty NLDM library
   (`src/klayout_tools/liberty_writer.py`) and written, then the emitted file
   is parsed back through `native/statime`'s own Liberty reader before the
   run reports success.

### Input slew: what `index_1` means and what is applied

`index_1` values are Liberty input *transition times*: the time between the
library's slew thresholds (`slew_lower_threshold_pct_rise` …
`slew_upper_threshold_pct_rise`, default 20% → 80%), scaled by
`slew_derate_from_library`. The applied stimulus is a **linear rail-to-rail
ramp** whose duration is chosen so its threshold-to-threshold time equals the
requested index value:

```
ramp_duration = index_1_value / slew_derate_from_library / ((upper_pct - lower_pct) / 100)
```

With the defaults that is `index / 0.6`. A linear ramp is not the same
stimulus a commercial characterizer applies (those drive the pin from a
*driving cell* of a declared size, which gives a realistically curved edge),
so expect a small, systematic difference from a vendor table — measured
against IHP's own `sg13g2_inv_1` typ corner it was within a few percent
across the 7x7 grid (see "Sanity check against a vendor `.lib`" below).
Quantifying that arc-by-arc is issue #2503's job, not this command's claim.

### Transient window and the settle check

The window is `settle → rise edge → settle → fall edge → settle`, with
`settle` defaulting to `max(2 ns, 3 x the longest ramp)`. If the window is
too short, a `.meas` card still *finds* a crossing — it just measures against
a starting level that never reached the rail — so an under-sized window
produces a table that is **wrong rather than absent**. The run therefore
checks each grid point's own measured `delay + transition` against 90% of the
settle window and fails with a named error, naming the offending point, if
any exceeds it. Raise `options.settle_ns` and re-run.

`options.tran_step_ns` (default `min(1 ps, shortest ramp / 10)`) is pinned as
ngspice's `tmax` too, not just its suggested step: the smallest grid
transition is tens of picoseconds, and ngspice's default max step would
resolve it with a handful of timepoints, making the `.meas` interpolation
error comparable to the quantity being measured.

### Negative delays are legitimate

A strong cell driven by a slow input edge into a light load can finish
switching *before* the input crosses its 50% threshold, so `cell_rise`/
`cell_fall` can legitimately be slightly negative at the slow-slew /
light-load corner of the grid. That is a real property of a 50%-threshold
delay definition, not a measurement failure; real libraries bound it with
`default_max_transition` rather than by clamping. This command emits what it
measured and never clamps.

### Sequential cells are refused

There is no attempt to characterize a flip-flop or latch. A sequential
cell's output `function` references an internal state node (`IQ`, `IQN`) that
is not a declared input pin, and that is a hard error:

```
klt characterize: cell 'dff_demo' pin 'Q': Liberty function "IQ" references
'IQ', which is not a declared input pin of this cell (declared: CLK, D) -- a
sequential cell or missing pin metadata, not a combinational arc this command
can characterize
```

A refusal is the point: a setup/hold constraint is a fundamentally different
measurement (a bisection search over a clock/data separation, not a grid
sweep), and silently characterizing a clock-to-Q path as a combinational arc
would produce a `.lib` that looks complete and is unusable.

### Multiple corners

One invocation, one corner, one file. Run the command once per corner, each
with its own `corner` block and `output.lib`, and let the consumer pick the
file — exactly how a vendor library ships (`sg13g2_stdcell_typ_1p20V_25C.lib`
and friends are separate files). Multi-corner Liberty is not a shape this
command emits.

## OSDI (Verilog-A) model preload

ngspice can only instantiate a Verilog-A compact model through an OSDI shared
library, and several open PDKs ship their core devices that way — IHP's
`sg13g2` MOSFETs are PSP103 Verilog-A, compiled to
`libs.tech/ngspice/osdi/*.osdi` by `scripts/fetch-sg13g2-sim-toolchain.sh`
(see [`pdks/README.md`](../../pdks/README.md)). Without the preload, every
device in the cell's netlist fails with `Unable to find definition of model
…` and the whole grid errors.

`options.osdi_preload` is a list of `.osdi` paths the generated testbench
emits as `pre_osdi` commands. They land in a `.control` block, because
`pre_osdi` is a *control* command (`pre_`-prefixed commands run before the
circuit is parsed, wherever the block sits) — which is the one place this
command's generated netlist deviates from [`sim.md`](sim.md)'s "a circuit
body, not a full deck" convention. That is a deliberate, documented
deviation, not an accident: `klt sim` generates its own `.control` block and
exposes no hook for adding lines to it. See issue #2513 for giving `klt sim`
a first-class model-preload option so this deck stops needing one.

## Request

```json
{
  "cell": {
    "name": "sg13g2_nand2_1",
    "subckt": "sg13g2_nand2_1",
    "netlist": "sg13g2_stdcell.spice",
    "pins": [
      { "name": "A", "direction": "input", "capacitance_pf": 0.0018 },
      { "name": "B", "direction": "input", "capacitance_pf": 0.0018 },
      { "name": "Y", "direction": "output", "function": "!(A*B)" }
    ],
    "power_pins": { "vdd": "VDD", "gnd": "VSS" },
    "area": 6.6
  },
  "corner": {
    "name": "typ_1p20V_25C",
    "process": "mos_tt",
    "supply_v": 1.2,
    "temperature_c": 25,
    "nom_process": 1
  },
  "grid": {
    "input_transition_ns": [0.0186, 0.0966, 0.174, 0.3294, 0.6408, 1.263, 2.5074],
    "output_load_pf": [0.001, 0.0234, 0.039, 0.0648, 0.108, 0.18, 0.3]
  },
  "models": {
    "pdk": "ihp-sg13g2",
    "lib": "libs.tech/ngspice/models/cornerMOSlv.lib"
  },
  "library": { "name": "sg13g2_nand2_1_typ_1p20V_25C" },
  "output": { "lib": "out/sg13g2_nand2_1_typ_1p20V_25C.lib" },
  "options": {
    "osdi_preload": ["$PDK_ROOT/ihp-sg13g2/libs.tech/ngspice/osdi/psp103.osdi"],
    "timeout_s": 900
  }
}
```

### `cell` (required)

| Field | Type | Description |
|---|---|---|
| `name` | string, **required** | The cell's name. Used as the `cell()` group name in the emitted `.lib` and, unless `subckt` overrides it, as the SPICE subcircuit to instantiate. |
| `netlist` | string, **required** | SPICE file defining the cell's `.subckt` — post-`klt pex` where available, a schematic/`spice/`-view netlist otherwise. May be a whole library file containing many `.subckt`s; only the named one is instantiated. |
| `subckt` | string | Subcircuit name, when it differs from `name`. |
| `pins` | array, **required** | One entry per **signal** terminal. See below. |
| `power_pins` | object, **required** | `{"vdd": "<terminal>", "gnd": "<terminal>"}` naming which subcircuit terminals are supply rails. Extra keys are allowed for a multi-rail cell and are bound to `vdd`/`0` by whether the key name contains a `v`. |
| `area` | number | Emitted as the cell's `area` attribute. Omitted from the `.lib` when absent. |

`pins[]` entries:

| Field | Type | Description |
|---|---|---|
| `name` | string, **required** | Must be a terminal of the subcircuit. |
| `direction` | `"input"` \| `"output"`, **required** | `inout` is out of scope. |
| `function` | string, **required for outputs** | The pin's Liberty boolean expression over the declared input pins. Accepted syntax: `( )`, prefix `!`, postfix `'`, AND (`&`, `*`, or juxtaposition), `^`, OR (`+`, `|`), and the constants `0`/`1`. |
| `capacitance_pf` | number | Echoed into the `.lib` as `capacitance`. **Not measured** — omitted from the emitted file when absent rather than fabricated. |

Every subcircuit terminal must be accounted for by `pins` + `power_pins`:
an unaccounted terminal is an error, because the generated testbench would
otherwise leave it floating, and a floating input on a CMOS gate is not a
valid operating point. An input pin the output's `function` does not
reference (an unused pin, or a second output's input on a multi-output cell)
is tied **low**.

### `corner` (required)

| Field | Type | Description |
|---|---|---|
| `name` | string, **required** | Corner label; becomes the `.lib`'s `operating_conditions` group name and `default_operating_conditions`. |
| `supply_v` | number, **required** | Supply voltage. Drives both the testbench supply and the `.lib`'s `nom_voltage`. |
| `temperature_c` | number, **required** | Simulated temperature; the `.lib`'s `nom_temperature`. |
| `process` | string | Model-library `.lib` section to select (e.g. `"mos_tt"`, `"tt"`). Requires `models`. Omit for a model library with no sections. |
| `nom_process` | number | The `.lib`'s `nom_process`/`operating_conditions.process`. Default `1`. |

### `grid` (required)

| Field | Type | Description |
|---|---|---|
| `input_transition_ns` | array of number, **required** | The `index_1` axis, in nanoseconds. Must be non-empty, positive, and strictly ascending — an NLDM index axis is interpolated over, so a repeated or out-of-order entry silently corrupts every lookup against it. |
| `output_load_pf` | array of number, **required** | The `index_2` axis, in picofarads. Same constraints. |

A good default is the grid the vendor library for the same process uses — for
`sg13g2_stdcell` that is the 7x7 `index_1`/`index_2` pair shown above, copied
from its own `TIMING_DELAY_7x7ds1` template.

### `models` (optional, same shape as `klt sim`'s)

Forwarded to `klt sim` unchanged except that a relative `lib` is re-anchored
onto *this* request's directory (the generated sim request lives in the output
directory). See [`sim.md`](sim.md)'s "Model library resolution" for the two
accepted shapes. Required when `corner.process` is given.

### `thresholds` (optional)

Liberty measurement thresholds, defaulting to the common open-PDK convention
(and IHP `sg13g2_stdcell`'s own): 50% delay thresholds, 20/80% slew
thresholds, no slew derate. Every field is emitted into the `.lib` header, so
a reader interpolating the tables uses the same definitions they were
measured with.

`input_threshold_pct_rise`, `input_threshold_pct_fall`,
`output_threshold_pct_rise`, `output_threshold_pct_fall`,
`slew_lower_threshold_pct_rise`, `slew_lower_threshold_pct_fall`,
`slew_upper_threshold_pct_rise`, `slew_upper_threshold_pct_fall`,
`slew_derate_from_library`. Each `slew_lower`/`slew_upper` pair must satisfy
`0 < lower < upper < 100`; an unknown field name is an error rather than a
silent no-op.

### `arcs` (optional override)

Normally omitted — arcs are derived from `function`. Given, it replaces
derivation entirely, for a cell whose function this command cannot derive
from or a caller who wants a specific side-input state measured:

```json
"arcs": [
  {
    "output_pin": "Y",
    "related_pin": "A",
    "timing_sense": "negative_unate",
    "measured_sense": "negative_unate",
    "side_inputs": { "B": true }
  }
]
```

`timing_sense` is what the `.lib` declares (`positive_unate` /
`negative_unate` / `non_unate`); `measured_sense` (default: `timing_sense`)
says which input edge produces the *rising* output edge under this arc's own
`side_inputs`, so it must itself be unate.

### `library` / `output` / `options` (optional)

| Field | Type | Description |
|---|---|---|
| `library.name` | string | The `library()` group name. Default `<cell name>_<corner name>`. |
| `output.outdir` | string | Where generated artifacts go. Default `.klt/characterize/` next to the request. `-o` overrides. |
| `output.lib` | string | The emitted `.lib` path. Default `<outdir>/<library name>.lib`. `--lib` overrides. |
| `options.settle_ns` | number | Settle window per edge. Default `max(2, 3 x longest ramp)`. See "Transient window". |
| `options.tran_step_ns` | number | Transient step, also used as `tmax`. Default `min(0.001, shortest ramp / 10)`. |
| `options.timeout_s` | number | Per-corner ngspice timeout. Default `900`. |
| `options.keep_artifacts` | boolean | Keep the ngspice log/deck. Default `false`; `--keep-artifacts` overrides. |
| `options.osdi_preload` | array of string | `.osdi` shared libraries to `pre_osdi`. See "OSDI model preload". |
| `netlist_source` | string | Forwarded to `klt sim` (`"schematic"` / `"extracted"`), for provenance on a post-extraction run. |

### Non-unate arcs

An XOR's `A -> Y` arc is `non_unate`: whether a rising `A` raises or lowers
`Y` depends on `B`. The emitted `timing_sense` reflects that (it is derived
from *every* sensitizing vector), but the tables are measured at **one**
side-input state — the first sensitizing vector, reported in the response's
`arcs[].side_inputs`. A commercial characterizer would split such an arc into
`when`-qualified groups; this command does not, and the response says which
state it measured so the limitation is visible rather than implied.

## Response

```json
{
  "schema_version": 1,
  "cell": {
    "name": "nand2_demo",
    "subckt": "nand2_demo",
    "netlist": { "path": "examples/characterize/cells.spice", "scope": "repo" },
    "pins": [
      { "name": "A", "direction": "input", "function": null, "capacitance_pf": 0.002 },
      { "name": "B", "direction": "input", "function": null, "capacitance_pf": 0.002 },
      { "name": "Y", "direction": "output", "function": "!(A*B)", "capacitance_pf": null }
    ]
  },
  "corner": {
    "name": "tt_1p80V_25C",
    "process": "tt",
    "supply_v": 1.8,
    "temperature_c": 25.0
  },
  "grid": {
    "input_transition_ns": [0.02, 0.08, 0.24, 0.72],
    "output_load_pf": [0.005, 0.03, 0.1, 0.3],
    "points": 16,
    "arc_count": 2,
    "measurement_count": 128
  },
  "thresholds": {
    "input_threshold_pct_rise": 50.0,
    "output_threshold_pct_rise": 50.0,
    "slew_lower_threshold_pct_rise": 20.0,
    "slew_upper_threshold_pct_rise": 80.0,
    "slew_derate_from_library": 1.0
  },
  "arcs": [
    {
      "output_pin": "Y",
      "related_pin": "A",
      "timing_sense": "negative_unate",
      "measured_sense": "negative_unate",
      "side_inputs": { "B": true },
      "cell_rise": {
        "index_1": [0.02, 0.08, 0.24, 0.72],
        "index_2": [0.005, 0.03, 0.1, 0.3],
        "values": [[0.0261, 0.0704, 0.175, 0.483]]
      },
      "cell_fall": { "index_1": [], "index_2": [], "values": [] },
      "rise_transition": { "index_1": [], "index_2": [], "values": [] },
      "fall_transition": { "index_1": [], "index_2": [], "values": [] }
    }
  ],
  "liberty": {
    "path": "/abs/path/.klt/characterize/characterize_demo_tt_1p80V_25C.lib",
    "library_name": "characterize_demo_tt_1p80V_25C",
    "cell_count": 1,
    "time_unit": "1ns",
    "capacitive_load_unit": [1.0, "pf"],
    "roundtrip": {
      "engine": "klt_statime_native",
      "status": "pass",
      "message": "parsed and interpolated: worst probe-path delay 0.0789 ns through 1 cell(s)",
      "probe_netlist": "/abs/path/.klt/characterize/roundtrip-probe.v"
    }
  },
  "simulation": {
    "testbench": "/abs/path/.klt/characterize/testbench.spice",
    "request": "/abs/path/.klt/characterize/sim-request.json",
    "report": "/abs/path/.klt/characterize/sim-report.json",
    "corner_id": "tt/1.800V/25C",
    "status": "pass",
    "runtime_s": 2.374,
    "engine": "ngspice",
    "engine_version": "46",
    "window": { "settle_ns": 3.6, "tran_step_ns": 0.001 }
  },
  "provenance": {
    "klt_version": "0.6.0",
    "klayout_version": "0.30.10",
    "pdk": null,
    "deck": null,
    "input": { "content_hash": "sha256:<hex>", "role": "netlist" }
  }
}
```

(The `arcs[]` block above is trimmed — every entry carries all four tables,
each a full `len(index_1) x len(index_2)` `values` matrix.)

| Field | Type | Description |
|---|---|---|
| `schema_version` | integer | Version of this command's JSON shape (`1`), per-command per [`../json-contract.md`](../json-contract.md). |
| `cell` | object | Echo of the resolved cell: `name`, `subckt`, `netlist` (the `{path, scope}` envelope — an *input*, same shape as `klt sim`'s own `netlist`), and `pins[]` as resolved. |
| `corner` | object | Echo of the characterized corner. |
| `grid` | object | The two index axes plus derived counts: `points` (`len(index_1) * len(index_2)`), `arc_count`, `measurement_count` (`points * arc_count * 4`). |
| `thresholds` | object | Every resolved Liberty threshold, defaults included — the definitions the tables mean. |
| `arcs` | array | One entry per characterized arc: `output_pin`, `related_pin`, `timing_sense`, `measured_sense`, `side_inputs` (the full held state, including inputs the function does not reference), and the four tables as `{index_1, index_2, values}` in the library's own units (ns / pF). Same numbers as the `.lib`, so a consumer need not re-parse Liberty to read them. |
| `liberty` | object | `path` (the emitted file — a **plain absolute string**, see below), `library_name`, `cell_count`, `time_unit`, `capacitive_load_unit`, and `roundtrip`. |
| `liberty.roundtrip` | object | `{engine, status, message, probe_netlist}`. See "Round-trip verification". |
| `simulation` | object | The generated artifacts (plain absolute strings) plus the `klt sim` corner's own `corner_id`/`status`/`runtime_s`, the engine and its version, and the resolved transient `window`. |
| `provenance` | object | The shared reproducibility block from [`../json-contract.md`](../json-contract.md). `pdk` is best-effort from `models.pdk`/`pdk_root` (else `null`); `input` pins the cell netlist (`role: "netlist"`). |

**Path-field shapes.** `cell.netlist` uses the `{path, scope}` envelope
because it is an *input* being pinned (matching `klt sim`'s own `netlist`).
`liberty.path`, `simulation.testbench`/`request`/`report`, and
`roundtrip.probe_netlist` are **plain absolute strings** because they are
generated artifacts a caller opens or chains onward — the same role
`klt extract`'s `netlist_path` and `klt sim`'s `corners[].artifacts.*` fill.
Both shapes and the per-field inventory are documented in
[`../json-contract.md`](../json-contract.md)'s "Output-artifact path fields".
A committed report must therefore not carry this envelope verbatim; see that
document's "Repo-relative provenance in *committed* artifacts".

There is deliberately **no top-level `status`**: a characterization is data,
not a verdict. The run either produced a complete model (exit `0`) or it
failed (exit `1`); there is no partial table to grade, because a missing grid
point aborts the run before anything is written.

## Round-trip verification

Before reporting success, the emitted `.lib` is parsed back through
`native/statime`'s own Liberty reader — the same `liberty.rs`/`nldm.rs` that
`klt synthesize`'s `sta` field already depends on. The extension's only
Python entry point is `critical_path_json(netlist, liberty, top, …)`, so the
check writes a one-instance structural-Verilog wrapper around the
characterized cell (`roundtrip-probe.v`, kept for inspection) and asks the
engine to analyse it. That exercises strictly more than a bare parse:
`liberty.rs` must parse the file, `sta.rs` must find the cell and its pins,
and `nldm.rs` must interpolate each table to produce a delay — so a file that
parses structurally but carries a malformed table still fails.

| `roundtrip.status` | Meaning |
|---|---|
| `pass` | The reader accepted the file and the engine reported a path delay through the cell. |
| `skipped` | The `klt_statime_native` extension is not installed, so **nothing was verified**. `message` says so explicitly. Never a fabricated pass. |
| `fail` | The reader (or the engine behind it) rejected the file. The run exits `1`; the file is left on disk for inspection. |

To get `pass` on a dev machine, build the extension:
`uv sync --extra dev --group statime` (or `maturin develop --release` inside
`native/statime/`).

## Sanity check against a vendor `.lib`

Characterizing IHP's own `sg13g2_inv_1` at `typ_1p20V_25C` on the vendor's
own 7x7 grid, against `sg13g2_stdcell_typ_1p20V_25C.lib`'s `cell_rise`
(ns, `index_1` = 0.0186 row and 2.5074 row):

| | `index_2` = 0.001 | 0.0234 | 0.039 | 0.0648 | 0.108 | 0.18 | 0.3 |
|---|---|---|---|---|---|---|---|
| vendor, slew 0.0186 | 0.02056 | 0.08490 | 0.12814 | 0.19952 | 0.31891 | 0.51817 | 0.84949 |
| `klt characterize` | 0.01941 | 0.08266 | 0.12574 | 0.19684 | 0.31580 | 0.51398 | 0.84426 |
| vendor, slew 2.5074 | 0.10308 | 0.50730 | 0.68076 | 0.90493 | 1.19043 | 1.56201 | 2.05537 |
| `klt characterize` | 0.10320 | 0.50494 | 0.67300 | 0.88860 | 1.16262 | 1.50333 | 1.92456 |

Deltas run from +0.1% to −6.4%, largest at the slow-slew / heavy-load corner
— consistent with the linear-ramp-vs-driving-cell stimulus difference noted
above. **This is an eyeball sanity check, not an accuracy claim**; the
arc-by-arc comparison against a vendor library is issue #2503's acceptance
criterion.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | The cell was characterized and the `.lib` was written. |
| `1` | Failed to run: malformed request, unresolvable/unparseable netlist, pin metadata that does not describe a combinational cell, a simulation that did not complete, a grid point with no measured value, a transient window too short, or an emitted `.lib` the round-trip reader rejects. |
| `2` | Argparse usage error (as with every other `klt` subcommand). |

There is no pass/fail code — see "no top-level `status`" above.

## Worked example

[`examples/characterize/`](../../examples/characterize/) characterizes a
synthetic 2-input NAND with no PDK and no Docker — just `ngspice`:

```bash
klt characterize examples/characterize/request.json
```

See its [README](../../examples/characterize/README.md) for what to look at
in the generated testbench and `.lib`.

### Against a real library (`sg13g2_stdcell`)

```bash
# Once: fetch the PDK and compile its Verilog-A models to OSDI.
scripts/fetch-ihp-sg13g2.sh
scripts/fetch-sg13g2-sim-toolchain.sh
```

Then a request like the one under "Request" above, with `cell.netlist`
pointing at
`libs.ref/sg13g2_stdcell/spice/sg13g2_stdcell.spice`, `models.lib` at
`libs.tech/ngspice/models/cornerMOSlv.lib`, `corner.process` at `mos_tt`, and
`options.osdi_preload` listing the four compiled `.osdi` files. A 7x7 grid
for a 2-input cell is roughly a minute of ngspice on one core.

## See also

- [`sim.md`](sim.md) — the corner/deck machinery this command drives, and the
  meaning of `models`, `netlist_source`, and the backends.
- [`sta.md`](sta.md) / [`synthesize.md`](synthesize.md) /
  [`place-and-route.md`](place-and-route.md) — the read side of the Liberty
  path this command writes into.
- [`pex.md`](pex.md) — producing the post-extraction netlist worth
  characterizing in the first place.
- [`../json-contract.md`](../json-contract.md) — the shared envelope,
  provenance block, and path-field shapes.
