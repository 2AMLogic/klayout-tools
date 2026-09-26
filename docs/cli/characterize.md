# `klt characterize`

SPICE-driven NLDM timing **and power** characterization of one standard cell
-- or a batch of them -- at **one** PVT corner, emitting a single Liberty
(`.lib`) model (issues #2502, #2503).

```
klt characterize <request> [-o OUTDIR] [--lib PATH] [--backend BACKEND]
                          [--keep-artifacts] [--cell NAME ...]
                          [--compare-to LIB] [--format text|json]
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
- `--cell` — characterize only the named cell out of a batch request's
  `cells` array. Repeatable; default every declared cell. A name the request
  does not declare is an error (never a silent no-op), and `--cell` on a
  single-cell (`cell`) request is an error too.
- `--compare-to` — a reference Liberty file (typically the vendor library for
  the same corner) to compare the emitted `.lib` against arc by arc. The
  result lands in the response's `comparison` block; see "Accuracy against a
  vendor library".
- `--format` — `text` (default) or `json`.

Requires `ngspice` on `$PATH` (it is `klt sim` that runs it — see
[`sim.md`](sim.md)). Needs no KLayout, no Docker, and no PDK unless the
request's `models` block names one.

## Scope of this command

**In scope**: one cell or a batch of cells (one combined `.lib`); one PVT
corner; every *combinational* timing arc of each cell; `cell_rise`,
`cell_fall`, `rise_transition`, `fall_transition`, `related_pin`,
`timing_sense`, `timing_type : combinational`; per-arc `internal_power()`
with `rise_power`/`fall_power`; per-cell `cell_leakage_power` and one
`leakage_power()` group per input state.

**Out of scope**, deliberately — read the absence of a group as "this
command did not measure it", never as an assertion about the cell:

| Not emitted | Where it belongs |
|---|---|
| more than one corner per invocation / per file | invoke once per corner; see "Multiple corners" below |
| sequential-cell constraint arcs (`setup_*`/`hold_*`/`recovery_*`), `ff`/`latch` groups | not yet filed — a sequential cell is **refused**, not mis-characterized (see "Sequential cells are refused") |
| measured pin capacitance | issue #2512 — `pins[].capacitance_pf` is echoed from the request when given and the attribute is omitted when it is not |
| `when`-qualified arc/power splitting, `bus`/`bundle` pins, input-pin (hidden) `internal_power` | not yet filed |

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
3. **Four timing `.meas` cards per grid point.** Delay is measured
   input-threshold → output-threshold; transition is measured between the
   library's own slew thresholds on the output edge. Which *input* edge
   produces the rising output edge follows the arc's measured polarity, so a
   `negative_unate` arc's `cell_rise` is correctly triggered by the input's
   *falling* edge.
4. **Four power `INTEG` cards per grid point, on the same instances.** Each
   instance reaches the shared rails through its own 0 V ammeter pair, so
   the charge each rail delivers to *that* instance over *that* edge is one
   `.meas tran ... INTEG` card away -- additive instrumentation on the same
   run, not a second simulation campaign. See "Power and leakage".
5. **Leakage on `2**n` static instances, same deck.** One never-switching
   instance per input state, its supply through its own ammeter; the average
   supply current over the run is that state's static current.
6. **Emit and verify.** Tables are reshaped into a Liberty NLDM library
   (`src/klayout_tools/liberty_writer.py`) and written, then the emitted file
   is parsed back through `native/statime`'s own Liberty reader before the
   run reports success.

In batch mode steps 1-5 run once **per cell** (one `klt sim` corner per
cell, artifacts under `<outdir>/cells/<cell>/`), and step 6 runs once over
the combined library. See "Batch mode".

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
across the 7x7 grid. "Accuracy against a vendor library" below quantifies
it arc by arc for a handful of cells.

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

## Power and leakage

### Internal energy: `rise_power` / `fall_power`

Every arc carries an `internal_power()` group on its output pin, with
`related_pin` equal to the arc's and `rise_power`/`fall_power` tables over the
**same** `index_1` x `index_2` axes as the timing tables (emitted against a
`power_lut_template`, `input_transition_time` x
`total_output_net_capacitance`). Values are in Liberty's internal-energy unit,
`voltage_unit**2 * capacitive_load_unit` -- **pJ** for this command's fixed
1 V / 1 pF units.

The tables hold **internal** energy, not total energy: a Liberty power
consumer (`klt power`, OpenSTA) adds the load's own `C_load * V**2 / 2` per
output transition on top of the table, so the table must exclude it or the
load would be counted twice. Per output edge:

```
E_edge = V * (Q_vdd + Q_gnd) / 2  -  C_load * V**2 / 2
```

`Q_vdd` is the charge drawn from the vdd rail and `Q_gnd` the charge returned
to the gnd rail over the window of the *input* edge that produces that output
edge (the same trigger choice the delay cards use, so a table never pairs one
edge's delay with the other edge's energy). Averaging the two rails is what
makes the rise/fall split symmetric: on an output rise `Q_vdd` carries the
load *and* internal-node charge while `Q_gnd` carries only short-circuit
current, and on a fall the reverse -- so each edge owns half the
internal-node energy plus its own short-circuit energy, and the two edges sum
to the whole-cycle internal energy exactly. (The alternative -- "vdd rail
only, subtract `C_load * V**2` on the rise" -- sums to the same cycle total
but piles all internal-node energy onto the rise edge. Measured against
IHP's `sg13g2_stdcell` it is the worse fit: mean per-point error ~2-5 fJ
against ~0.2-1.3 fJ for the symmetric split. The vendor split does differ on
arcs through a series stack -- see "Accuracy against a vendor library".)

Consequences worth knowing:

- At heavy loads an entry is a small difference of two large numbers (a
  ~2 fJ internal energy out of ~220 fJ of rail energy at 0.3 pF / 1.2 V), so
  it carries the integration's noise; nothing is clamped, and a slightly
  negative entry would be emitted as measured (none occurred on the
  `sg13g2_stdcell` comparison below).
- Supply leakage integrated over the (settle-dominated) window is left in:
  tens to ~100 pW over a 7x7 grid's ~17 ns window is a few attojoules, three
  orders of magnitude below the femtojoule internal energy.

### Leakage: `cell_leakage_power` / `leakage_power`

For each of the cell's `2**n` input states a never-switching instance is
added to the same deck, its vdd terminal -- and every input held **high** --
fed through one ammeter; low inputs are tied to ground. The state's static
power is `V * I_avg` (in pW, the library's `leakage_power_unit`). Routing the
high inputs through the ammeter matters: their gate leakage is drawn from the
supply too, and measured on `sg13g2_inv_1` with `A` high, leaving it out
under-reports the state by roughly a third.

Each state becomes a `leakage_power()` group whose `when` is the conjunction
of its input literals (`"A&!B"`), and `cell_leakage_power` is the unweighted
mean over all states -- the convention IHP's own library uses (its
`sg13g2_inv_1` reports 63.0032 = the mean of 82.469 and 43.5374).

## Batch mode

A request carries either `cell` (one object) **or** `cells` (a non-empty
array of the same objects) -- both, or neither, is an error. Every cell in a
batch shares the request's one `corner`, `grid`, `thresholds`, `models`, and
`options`, which is what lets their tables live in one library:

```json
{
  "cells": [
    { "name": "sg13g2_inv_1",   "netlist": "sg13g2_stdcell.spice", "pins": [...], "power_pins": {...} },
    { "name": "sg13g2_nand2_1", "netlist": "sg13g2_stdcell.spice", "pins": [...], "power_pins": {...} }
  ],
  "corner": { ... },
  "grid": { ... }
}
```

- The mechanism is #2502's single-cell run **in a loop**: one `klt sim` corner
  per cell, each cell's testbench/sim request/sim report under
  `<outdir>/cells/<cell>/`, then one combined `.lib` and one round-trip check
  over it.
- **Every cell is validated before any is simulated** -- a typo in the fifth
  cell's pin list does not cost the first four cells' simulation time.
- **All or nothing.** If any cell fails, nothing is emitted and the error
  names the cell (`cell 'sg13g2_nor2_1': ...`). Validation errors name the
  entry (`request.cells[3].pins ...`).
- Cell names must be unique (a library holds one `cell()` group per name).
- The single-cell `request.arcs` override is refused in batch mode; put an
  `arcs` array on the `cells[]` entry it applies to instead.
- `library.name` defaults to `klt_characterize_<corner name>` (the
  single-cell default names the cell).
- `--cell NAME` (repeatable) characterizes a subset of the declared cells, in
  request order.

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

### `cell` / `cells` (exactly one required)

`cell` is one cell object; `cells` is a non-empty array of them (see "Batch
mode"). In batch mode each entry may also carry its own `arcs` override (same
shape as the request-level one below). The object's fields:

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

Single-cell form only (in batch mode, put `arcs` on the `cells[]` entry).
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
| `library.name` | string | The `library()` group name. Default `<cell name>_<corner name>` (single cell) or `klt_characterize_<corner name>` (batch). |
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
    "measurement_count": 128,
    "power_measurement_count": 128,
    "leakage_measurement_count": 4,
    "total_measurement_count": 260
  },
  "thresholds": {
    "input_threshold_pct_rise": 50.0,
    "output_threshold_pct_rise": 50.0,
    "slew_lower_threshold_pct_rise": 20.0,
    "slew_upper_threshold_pct_rise": 80.0,
    "slew_derate_from_library": 1.0
  },
  "units": { "time": "ns", "capacitance": "pF", "internal_energy": "pJ", "leakage_power": "pW" },
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
      "fall_transition": { "index_1": [], "index_2": [], "values": [] },
      "rise_power": {
        "index_1": [0.02, 0.08, 0.24, 0.72],
        "index_2": [0.005, 0.03, 0.1, 0.3],
        "values": [[0.0185, 0.0223, 0.0248, 0.0259]]
      },
      "fall_power": { "index_1": [], "index_2": [], "values": [] }
    }
  ],
  "leakage": {
    "cell_leakage_power_pw": 104.3,
    "states": [
      { "when": "!A&!B", "inputs": { "A": false, "B": false }, "value_pw": 3.25 },
      { "when": "!A&B", "inputs": { "A": false, "B": true }, "value_pw": 3.25 },
      { "when": "A&!B", "inputs": { "A": true, "B": false }, "value_pw": 404.2 },
      { "when": "A&B", "inputs": { "A": true, "B": true }, "value_pw": 6.51 }
    ]
  },
  "cells": [
    { "cell": { "name": "nand2_demo", "...": "..." }, "arcs": [], "leakage": {}, "simulation": {} }
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
  "comparison": null,
  "provenance": {
    "klt_version": "0.6.0",
    "klayout_version": "0.30.10",
    "pdk": null,
    "deck": null,
    "input": { "content_hash": "sha256:<hex>", "role": "netlist" }
  }
}
```

(The `arcs[]` block above is trimmed — every entry carries all six tables,
each a full `len(index_1) x len(index_2)` `values` matrix — and so is
`cells[]`, whose single entry here is the same `cell`/`arcs`/`leakage`/
`simulation` shown at top level.)

| Field | Type | Description |
|---|---|---|
| `schema_version` | integer | Version of this command's JSON shape (`1`), per-command per [`../json-contract.md`](../json-contract.md). |
| `corner` | object | Echo of the characterized corner. |
| `grid` | object | The two index axes plus derived counts, summed over every cell: `points` (`len(index_1) * len(index_2)`), `arc_count`, `measurement_count` (timing cards, `points * arc_count * 4` -- unchanged from #2502), `power_measurement_count` (`INTEG` rail-charge cards, `points * arc_count * 4`), `leakage_measurement_count` (one per input state per cell), and `total_measurement_count` (their sum). |
| `thresholds` | object | Every resolved Liberty threshold, defaults included — the definitions the tables mean. |
| `units` | object | The units of every value in this response and the `.lib`: `time` `ns`, `capacitance` `pF`, `internal_energy` `pJ`, `leakage_power` `pW`. |
| `cells` | array | One entry per characterized cell, in request order: `{cell, arcs, leakage, simulation}`, each shaped exactly like the single-cell top-level fields below. **This is the batch-mode data.** |
| `cell` | object \| null | Single-cell form: `cells[0].cell`. `null` in batch mode (there is no one cell to describe). |
| `arcs` | array \| null | Single-cell form: `cells[0].arcs`; `null` in batch mode. One entry per characterized arc: `output_pin`, `related_pin`, `timing_sense`, `measured_sense`, `side_inputs` (the full held state, including inputs the function does not reference), and the six tables `cell_rise`/`cell_fall`/`rise_transition`/`fall_transition`/`rise_power`/`fall_power` as `{index_1, index_2, values}` in the library's own units. Same numbers as the `.lib`, so a consumer need not re-parse Liberty to read them. |
| `leakage` | object \| null | Single-cell form: `cells[0].leakage`; `null` in batch mode. `cell_leakage_power_pw` (the mean over states) and `states[]` of `{when, inputs, value_pw}`, one per input state. |
| `liberty` | object | `path` (the emitted file — a **plain absolute string**, see below), `library_name`, `cell_count`, `time_unit`, `capacitive_load_unit`, and `roundtrip`. |
| `liberty.roundtrip` | object | `{engine, status, message, probe_netlist}`. See "Round-trip verification". |
| `simulation` | object \| null | Single-cell form: `cells[0].simulation`; `null` in batch mode. The generated artifacts (plain absolute strings) plus the `klt sim` corner's own `corner_id`/`status`/`runtime_s`, the engine and its version, and the resolved transient `window`. |
| `comparison` | object \| null | The `--compare-to` report (see "Accuracy against a vendor library"); `null` when not asked for. |
| `provenance` | object | The shared reproducibility block from [`../json-contract.md`](../json-contract.md). `pdk` is best-effort from `models.pdk`/`pdk_root` (else `null`); `input` pins the (first) cell's netlist (`role: "netlist"`). |

`cells[].cell` is the echo of the resolved cell: `name`, `subckt`, `netlist`
(the `{path, scope}` envelope — an *input*, same shape as `klt sim`'s own
`netlist`), and `pins[]` as resolved.

**Compatibility.** Every #2502 field is still present with its #2502 meaning
for a single-cell (`cell`) request; power tables, `leakage`, `units`,
`cells`, `comparison`, and the extra `grid` counts are additions. The
top-level `cell`/`arcs`/`leakage`/`simulation` are `null` only for a batch
(`cells`) request -- a request shape that did not exist before #2503, so no
existing consumer meets the nulls unawares.

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
check writes a structural-Verilog wrapper instantiating **every**
characterized cell once (`roundtrip-probe.v`, kept for inspection; batch
instances get private `u<i>_<pin>` ports) and asks the engine to analyse it.
A combined library the reader resolves fewer cells of than were
characterized fails the check, rather than passing on the strength of its
first cell. That exercises strictly more than a bare parse:
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

## Accuracy against a vendor library

`--compare-to LIB` diffs the emitted `.lib` against a reference Liberty file
arc by arc (`src/klayout_tools/liberty_compare.py`, usable on its own:
`liberty_compare.compare_libraries(ours, reference)`). IHP ships a vendor
`.lib` per corner for `sg13g2_stdcell`, characterized from the same SPICE
netlists and model cards this repo fetches, so characterizing a handful of
those cells and diffing against the vendor file is a self-checking accuracy
test that needs no new library (#2498's first milestone).

### What is compared

Per cell in the emitted file:

- every `timing()` group, matched by `(output pin, related_pin)`:
  `cell_rise`, `cell_fall`, `rise_transition`, `fall_transition`;
- every `internal_power()` group, matched the same way: `rise_power`,
  `fall_power`;
- every `leakage_power()` group, matched by **input state** — our `when`
  (`"A&!B"`) is expanded to the input assignments it covers and matched to the
  reference group whose own `when` holds there. A vendor `when` may name the
  output too (IHP writes `"A&!Y"`), so output values are computed from the
  reference cell's own `function`;
- `cell_leakage_power`.

Tables are compared point by point on **our** grid. A point on a reference
grid node (our axes equal to, or a subset of, the vendor's) reads the vendor
value directly; any other point is bilinearly interpolated from the
reference table, and that table reports `interpolated: true`.

**Nothing is silently omitted.** A group present on one side and not the
other lands in `missing`; every field reports how many points it actually
`compared`; and `summary.within_tolerance` is `true` only when every field
was compared at least once, every point is inside its bound, and `missing`
is empty — "never compared" is not "within tolerance". The text report prints
`NOT COMPARED` for a field with no compared points.

### The `comparison` block

```json
{
  "schema_version": 1,
  "ours": "/abs/out/klt_characterize_typ_1p20V_25C.lib",
  "reference": "/abs/.../sg13g2_stdcell_typ_1p20V_25C.lib",
  "tolerances": { "cell_rise": { "rel": 0.15, "abs": 0.005 }, "...": {} },
  "cells": [
    {
      "name": "sg13g2_nand2_1",
      "arcs": [
        {
          "output_pin": "Y",
          "related_pin": "A",
          "tables": {
            "cell_rise": {
              "interpolated": false, "points": 49, "violations": 0,
              "max_abs_delta": 0.14, "max_rel_delta": 0.092, "mean_rel_delta": 0.044,
              "worst": { "index_1": 1.263, "index_2": 0.3, "ours": 1.3906, "reference": 1.5308,
                         "delta": -0.1401, "rel_delta": -0.0915, "bound": 0.2296,
                         "tolerance_used": 0.610, "within_tolerance": true },
              "within_tolerance": true
            }
          }
        }
      ],
      "leakage": {
        "states": [ { "when": "A&!B", "reference_when": "A*!B", "ours": 38.31, "reference": 43.36, "...": "..." } ],
        "cell_leakage_power": { "ours": 77.37, "reference": 81.25, "...": "..." }
      },
      "missing": [],
      "notes": []
    }
  ],
  "summary": {
    "cell_count": 4,
    "fields": {
      "cell_rise": { "compared": 294, "violations": 0, "max_abs_delta": 0.215,
                     "max_rel_delta": 0.099, "worst": { "where": "sg13g2_nand2_1 B->Y cell_rise", "...": "..." },
                     "tolerance": { "rel": 0.15, "abs": 0.005 }, "within_tolerance": true }
    },
    "missing": [],
    "within_tolerance": true
  }
}
```

Every point entry carries `ours`, `reference`, `delta` (`ours - reference`),
`rel_delta` (`null` for a zero reference), `bound`
(`max(rel * |reference|, abs)`), `tolerance_used` (`|delta| / bound`; `<= 1`
passes), and `within_tolerance`. `notes` records anything the reader had to
choose (e.g. a second `when`-qualified group for one pin pair: the first is
compared, and the note says so). `ours`/`reference` are plain absolute
strings.

### The documented tolerance

A point passes when `|ours - reference| <= max(rel * |reference|, abs)` — a
relative bound with an absolute floor, because NLDM entries near zero (a 50%
delay at a slow edge into a light load; a femtojoule internal energy) make a
purely relative bound meaningless. The bounds are
`liberty_compare.DEFAULT_TOLERANCES`, and they are **not picked from thin
air**: #2503 asked for the bound an external reference (the EZ130 8T
characterization, arXiv:2609.29965v1) reports if it states one, else the
spread observed on the first comparison run. That paper was not available to
this build, so each bound below is the observed worst case, rounded up with
headroom, from characterizing `sg13g2_inv_1`, `sg13g2_buf_1`,
`sg13g2_nand2_1`, and `sg13g2_nor2_1` at `typ_1p20V_25C` on IHP's full 7x7
grid (294 points per table field, 12 leakage states, 4 cell totals) against
`sg13g2_stdcell_typ_1p20V_25C.lib`:

| Field | `rel` | `abs` | Observed worst | Observed p95 / median | Signed mean |
|---|---|---|---|---|---|
| `cell_rise` | 0.15 | 0.005 ns | 9.9% | 8.5% / 2.6% | −2.3% |
| `cell_fall` | 0.15 | 0.005 ns | 12.3% | 11.4% / 3.8% | −3.3% |
| `rise_transition` | 0.25 | 0.005 ns | 13.5% | 12.3% / 2.5% | −3.0% |
| `fall_transition` | 0.25 | 0.005 ns | 20.7% | 18.1% / 3.6% | −4.9% |
| `rise_power` | 0.50 | 0.003 pJ | 2.37 fJ | 90% / 16% (rel) | +21% |
| `fall_power` | 0.50 | 0.003 pJ | 2.44 fJ | 90% / 9% (rel) | +18% |
| `leakage_power` | 0.15 | 5 pW | 11.6% | — | −5% |
| `cell_leakage_power` | 0.10 | 5 pW | 5.2% | — | −4.9% |

What the residuals are, so a reader can tell a regression from the known
difference:

- **Timing is systematically a few percent fast, worst at slow input slews.**
  This command drives a *linear* rail-to-rail ramp; a commercial
  characterizer drives the pin from a driving cell, whose curved edge spends
  longer near the threshold. The gap grows with the input transition, which
  is where every worst-case point sits (the `index_1` = 1.263 / 2.5074 rows).
- **Internal energy agrees to ~2.5 fJ absolute, but its rise/fall split
  differs on arcs through a series stack.** For `sg13g2_nand2_1 B->Y` (B
  drives the bottom NMOS, so the stack's internal node is charged and
  discharged with the output) and `sg13g2_nor2_1 A->Y` (the PMOS mirror), the
  vendor attributes most of the internal node's energy to one edge while this
  command's symmetric split (see "Power and leakage") gives each edge half.
  The two arcs' vendor rise/fall pairs are about 1.9 / 4.1 fJ and 4.3 /
  2.0 fJ; ours are about 3.4 / 3.4 fJ — a similar cycle total, a different
  split, and relative deltas near 200% on the small entry (per-table
  medians ~80%). The other arcs (`inv_1`, `buf_1`, `nand2_1 A->Y`,
  `nor2_1 B->Y`) agree to a per-table median of 2-10% (26% for
  `nand2_1 A->Y` `rise_power`). Separately, entries at the heaviest load
  read 0.4-1.2 fJ high on those arcs: there the table is a ~2 fJ
  difference of two ~220 fJ quantities (rail energy minus
  `C_load * V**2 / 2`), so a 0.5% disagreement in the load charge
  dominates. Hence a relative bound with a 3 fJ absolute floor: it
  holds every observed point without pretending the per-edge split is a
  settled question. The cause of the stacked-arc split is tracked in issue
  #2527 rather than tuned away here.
- **Leakage is a few percent low**, consistently across states; the worst
  state (`sg13g2_nand2_1` `A&!B`, −11.6%) is the one whose static current
  flows through a partially-on stack.

### Enforced by a test

`tests/test_characterize_vendor_comparison.py` has two tiers:

- **Harness unit tests** (always run, no simulator): the reader, point-by-point
  deltas, the tolerance formula, grid subsets vs. interpolation, leakage
  matching against a vendor `when` that names the output, and — the "no field
  silently omitted" concern — that a reference lacking power or leakage
  groups reports them `missing` with `compared: 0` and fails the overall
  verdict.
- **The vendor comparison** characterizes the four cells above in one batch
  run (on a 3x3 subset of the vendor's 7x7 axes, so every point reads a
  vendor value directly and the run is minutes rather than a quarter hour),
  asserts every field of every arc and every leakage state was compared, and
  asserts **each field is within `DEFAULT_TOLERANCES`** (parametrized per
  field, so a regression names the field it broke). It also checks the
  combined `.lib` round-trips through `native/statime` and the
  `--compare-to` CLI text path. It needs `ngspice` plus a fetched IHP PDK with
  compiled OSDI models (`scripts/fetch-ihp-sg13g2.sh` +
  `scripts/fetch-sg13g2-sim-toolchain.sh`, or `KLT_IHP_SG13G2_ROOT` pointing
  at the `ihp-sg13g2` directory) and is skipped otherwise — CI does not fetch
  the PDK, the same local-only posture as `tests/test_pdk.py`'s real-PDK
  tier.

A change that moves any field outside its bound fails that test; widening a
bound means updating this table with the new observed spread and its cause.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Every requested cell was characterized and the `.lib` was written. With `--compare-to`, regardless of whether the comparison is within tolerance — the comparison is data, reported in `comparison.summary.within_tolerance`; the enforced bar is the automated test (see below). |
| `1` | Failed to run: malformed request, unresolvable/unparseable netlist, pin metadata that does not describe a combinational cell, a simulation that did not complete, a grid point with no measured value, a transient window too short, an emitted `.lib` the round-trip reader rejects, an unknown `--cell` name, or an unreadable `--compare-to` library. |
| `2` | Argparse usage error (as with every other `klt` subcommand). |

There is no pass/fail code — see "no top-level `status`" above.

## Worked example

[`examples/characterize/`](../../examples/characterize/) characterizes a
synthetic 2-input NAND with no PDK and no Docker — just `ngspice` — and, in
batch mode, an inverter plus the NAND into one library:

```bash
klt characterize examples/characterize/request.json
klt characterize examples/characterize/request-batch.json
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
for a 2-input cell is roughly a minute of ngspice on one core. Add
`--compare-to .../libs.ref/sg13g2_stdcell/lib/sg13g2_stdcell_typ_1p20V_25C.lib`
to diff the result against IHP's own library;
`tests/test_characterize_vendor_comparison.py`'s `_vendor_request` builds
exactly this request for four cells.

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
