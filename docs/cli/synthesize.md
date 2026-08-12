# `klt synthesize`

Synthesize RTL sources against a resolved standard-cell liberty via
Yosys + bundled ABC — Phase 2 of
[Epic #391](https://github.com/2AMLogic/klayout-tools/issues/391) ("adopt the
digital engine class — Yosys + OpenROAD — RTL→GDS as a first-class `klt`
flow").

```
klt synthesize <request> [--pdk VARIANT] [--pdk-root ROOT] \
    [--verify-equivalence] [--equiv-timeout-s SECONDS] [--format text|json]
```

This is the build phase carried by two accepted Phase 1 spikes — read them
first; where this document and the code disagree with either, this document
(and the code) win:

- [`docs/design/yosys-synthesis-spike.md`](../design/yosys-synthesis-spike.md)
  (#396) — the invocation shape (a generated `.ys` script, never the `-S`
  shortcut or an ever-growing `-p` string) and the output-parsing recipe
  (`stat -liberty <lib> -json -top <top>` captured via `tee -q -o <path>`,
  never re-derived by parsing `write_verilog`'s netlist).
- [`docs/design/digital-flow-contracts-spike.md`](../design/digital-flow-contracts-spike.md)
  section 4 (#399) — the request/response JSON contract and exit-code table
  this command implements.

Like `klt lvs`/`klt sim`, `klt synthesize` takes a **request document** — RTL
sources plus PDK/liberty selection plus optional constraints is richer than a
flag line carries cleanly — not positional RTL file args.

- `<request>` — a path to a request JSON file, e.g. `klt synthesize request.json`.
  Relative paths inside the request (`sources`) resolve against the
  **request file's own directory**.
- `--pdk` — PDK variant to resolve (e.g. `sky130A`); overrides `$PDK`.
  Optional — omit to use `find_pdk()`'s own default search order.
- `--pdk-root` — explicit PDK install root; overrides `$PDK_ROOT` and the
  search order.
- `--verify-equivalence` — gate the produced netlist through `klt equiv`
  against its own source RTL before returning; a non-equivalent or
  inconclusive verdict is a hard failure (exit 1). Off by default. See
  "Equivalence gate" below.
- `--equiv-timeout-s` — overall wall-clock timeout in seconds for the
  `--verify-equivalence` proof (default: `klt equiv`'s own `60`); no effect
  unless `--verify-equivalence` is given.
- `--format` — `text` (default, a human-readable summary) or `json`.

## Engine

`request.engine` is a data field, not a code path (contract spike section
2) — `"yosys"` is the only value implemented today; an unsupported value is
an application error (exit 1). Yosys + its bundled ABC technology mapper are
invoked as a **subprocess** (`yosys -s <script>`) — there is no Python
binding for Yosys the way `klayout.db` already is for `klt lvs`/`klt
extract`. Requires a `yosys` binary on `$PATH`; a missing binary is a clear,
actionable error (exit 1), never a traceback.

One post-processing stage is **not** Yosys: after `write_verilog` produces
the mapped netlist, `klt-statime-native` (a compiled Rust extension, called
in process — see "`sta`" below) analyzes that netlist's timing graph. It is
optional and additive: a missing extension reports `"sta": null` and changes
nothing else about the run.

## Generated `.ys` script

Per the Yosys survey's own recommendation, `klt synthesize` generates a
`.ys` script into `.klt/synthesize/` (next to the request file — the same
"next to the input" default `klt sim`'s `.klt/sim/` artifacts directory
already uses) and invokes `yosys -s <script>`, rather than string-
interpolating an ever-growing `-p` command line. The script is kept as a
debuggable, saveable artifact (`script_path` in the response), never
deleted — the same discipline `klt sim`'s generated corner decks already
follow.

The generated script is the Yosys survey's own verified pass sequence, with
the `abc` line carrying the constraint/exclusion flags described below:

```
read_verilog <source-1>
read_verilog <source-2>
...
hierarchy -check -top <hdl_toplevel>
synth -top <hdl_toplevel>
dfflibmap -liberty <resolved liberty path>
tee -q -o <abc log path> abc -liberty <resolved liberty path> -constr <constr path> [-D <picoseconds>] [-dont_use <glob> ...]
clean
[hilomap -hicell <tie-hi cell> <port> -locell <tie-lo cell> <port>]
tee -q -o <stats path> stat -liberty <resolved liberty path> -json -top <hdl_toplevel>
write_verilog -noattr <netlist path>
```

Every path embedded in the script is absolute, so the script runs correctly
regardless of the invoking process's own working directory.

### Artifacts written to `.klt/synthesize/`

| File | Contents |
| --- | --- |
| `synth_<top>.ys` | The generated Yosys script (`script_path` in the response). |
| `<top>_synth.v` | The mapped gate-level netlist (`netlist_path`). |
| `<top>_stats.json` | The captured `stat -liberty … -json` output this command parses for `instance_count`/`area_um2`. |
| `<top>_abc.constr` | The generated two-line ABC constraint file (`set_driving_cell` / `set_load`) — present only for a `cell_library` with a constraint-table entry (see below). |
| `<top>_abc.log` | The captured `abc` pass output, including ABC's own `stime -p` summary line this command parses into `timing` — same file, same `tee -q -o` discipline as the stats capture. |

### ABC constraints, delay target, and cell exclusions

`abc -liberty <lib>` **without** `-constr` skips ABC's own
`buffer; upsize; dnsize; stime -p` tail entirely (`yosys -p 'help abc'` —
those four steps are `-constr`-gated). That is the whole reason this command
emits a constraint file: one flag turns on load-driven buffering and
drive-strength sizing *and* produces the only delay number available
anywhere in this flow.

- **`-constr <top>_abc.constr`** is passed whenever the resolved
  `pdk.cell_library` has an entry in `synthesize.py`'s own
  `_ABC_CONSTR_INPUTS` table (`sky130_fd_sc_hd`,
  `gf180mcu_fd_sc_mcu9t5v0` today). Its `set_driving_cell`/`set_load`
  values are ORFS's own `ABC_DRIVER_CELL`/`ABC_LOAD_IN_FF` for that
  platform, cross-checked against the installed liberty — the same
  "verified, never guessed" per-library reference-data posture
  `place_and_route.py`'s `_CTS_BUFFER_CELLS`/`_ROUTING_LAYER_RANGE` tables
  already use. Each entry cites its source in that table's own docstring.
- **`-D <picoseconds>`** is passed **only** when the request supplies
  `constraints.clock_period_ns` (see the request table below for the
  `null` behaviour).
- **`-dont_use <glob>`**, once per entry, when the library has an entry in
  the `_ABC_DONT_USE_GLOBS` table. For `sky130_fd_sc_hd` that is
  `sky130_fd_sc_hd__lpflow_*` and `sky130_fd_sc_hd__probe*` — the
  power-domain isolation and test-probe cells ORFS's own sky130hd
  `DONT_USE_CELLS` excludes (the two globs match that 36-cell list exactly
  against the installed liberty). Without them, a plain `gcd` synthesis maps
  19 `lpflow_*` isolation cells into a design with no power domains.

A `cell_library` in **neither** table is never given a guessed driving cell
or exclusion list: its generated script keeps exactly the pre-#807 shape
(`abc -liberty <lib>`, no `tee`), and `timing` stays `null`.

**Yosys version note.** `abc -dont_use` does not exist in older Yosys
builds (verified: present in 0.68, absent in Ubuntu 24.04's 0.33, where
passing it is a hard error). This command probes `yosys -p 'help abc'` once
per run and omits the exclusion list on builds that do not support it,
rather than failing — the same graceful degradation `sequential_area_um2`
already applies to those builds. `-constr`/`-D`/`stime -p` are supported on
both.

### Constant ties (`hilomap`)

Yosys leaves constant drivers in a mapped netlist as bare Verilog literals
— `assign q[5] = 1'h0;`, `.D(1'h1)`. That is fine for simulation and
formal, and fatal for place-and-route: OpenSTA's Verilog reader materialises
one net per constant *value* it reads (conventionally `zero_` and `one_`),
OpenROAD types those nets `GROUND`/`POWER`, and TritonRoute refuses to route
a power/ground-typed net —

```
[ERROR DRT-0305] Net zero_ of signal type GROUND is not routable by TritonRoute. Move to special nets.
```

— which aborts the whole `route` stage before it does any work. Constant
ties are ubiquitous in real RTL, so this made every design that needs one
un-routable end to end (issue #854).

The generated script therefore ends with a `hilomap` pass that replaces
each constant driver with a real tie cell, whenever the resolved
`pdk.cell_library` has an entry in `synthesize.py`'s own `_TIE_CELLS`
table:

| `cell_library` | tie-high | tie-low |
| --- | --- | --- |
| `sky130_fd_sc_hd` | `sky130_fd_sc_hd__conb_1` `HI` | `sky130_fd_sc_hd__conb_1` `LO` |
| `gf180mcu_fd_sc_mcu9t5v0` | `gf180mcu_fd_sc_mcu9t5v0__tieh` `Z` | `gf180mcu_fd_sc_mcu9t5v0__tiel` `ZN` |

Both rows are ORFS's own `TIEHI_CELL_AND_PORT`/`TIELO_CELL_AND_PORT` for
that platform, cross-checked against the installed liberty (the sky130 cell
drives both constants from one instance; gf180mcu has two distinct cells,
and its `__filltie` is a well-tie filler, not a logic constant driver — the
sky130 shape is deliberately **not** carried over by analogy). This mirrors
where ORFS runs the same pass, at the end of its own `synth.tcl`.

Two consequences worth knowing:

- **`hilomap` runs before `stat`**, so the tie cells it inserts are counted
  in `instance_count`, `instance_counts_by_type`, and `area_um2` — they are
  real instances that occupy real area, not bookkeeping.
- **No `-singleton`.** ORFS collapses every constant onto one tie-hi and one
  tie-lo instance and splits that fanout back out at floorplan time with
  OpenROAD's `repair_tie_fanout` (which only duplicates tie cells that
  *already exist*). `klt place-and-route` runs no such step, so this command
  uses Yosys's default instead — one tie instance per constant bit — and
  leaves any residual high-fanout tie net to the `place` stage's existing
  `repair_design`.

A `cell_library` with no `_TIE_CELLS` entry emits no `hilomap` line at all,
rather than a guessed cell name. A design that needs no constant tie (the
repo's own `gcd.v`) is unaffected: its netlist and `stat` output are
byte-identical with and without the pass.

## PDK / liberty resolution

`request.pdk.cell_library`/`corner` are resolved to a liberty file via the
same `find_pdk()`/`libs_ref` discovery `klt pdk`/`klt cells` already use
(`src/klayout_tools/pdk.py`) — **no new PDK-fetch mechanism**. `sky130_fd_sc_hd`
liberty comes from a volare/open_pdks install, **not** this repo's fetched
`scripts/fetch-pdks.sh`/lambdapdk payload (which ships `sky130io`/
`sky130sram`, not the digital standard-cell library).

`request.pdk` carries only `cell_library`/`corner` — no `variant`/`root`
selector of its own. The resolved PDK install is whatever `find_pdk()`'s own
default search order finds (`$PDK_ROOT`/`$PDK` environment variables, then
the ciel/volare stores, then the conventional install prefixes) — exactly as
`klt pdk find` with no flags would resolve — unless the CLI's own `--pdk`/
`--pdk-root` flags (mirroring `klt extract`'s identical pair) pin a specific
installed variant/root instead.

- `pdk.cell_library` — a standard-cell library name. Not restricted to a
  single PDK family: any standard-cell library the resolved install ships a
  `libs_ref` entry for resolves the same way (`sky130_fd_sc_hd` and
  `gf180mcu_fd_sc_mcu9t5v0` both verified end to end).
- `pdk.corner` — a liberty corner selector (e.g. `tt_025C_1v80`); when
  omitted, the nominal (typical-process, room-temperature) corner
  `klt pdk cells`'s own `nominal_corner` selection already picks is used.

When the resolved install has no matching liberty at all — no PDK install
resolves, the install ships no `libs_ref` asset, `cell_library` is not
present under it, or the requested (or nominal-default) corner has no
matching `.lib` file — this is a clear **"liberty not found for deck"**
application error (exit 1), matching `klt drc`'s existing "deck requires an
asset the resolved install doesn't ship" posture.

## `timing`: ABC's own pre-layout estimate, **not** signoff STA

`timing` carries ABC's `stime -p` critical-path number — the one delay
figure this flow produces — shaped so it can never be mistaken for the
OpenSTA-backed number Phase 4's P&R step will eventually report:

```json
"timing": {
  "source": "abc_stime",
  "wire_load": null,
  "critical_path_ps": 2485.93,
  "delay_target_ps": null
}
```

**Read the caveats before using this number:**

- **It is wire-free.** ABC reports `WireLoad = "none"` on every run of this
  flow, echoed here as `"wire_load": null`. There is no placement, so there
  are no wire RCs in it at all — a real post-layout critical path will be
  longer, often substantially.
- **It is combinational, over the cone ABC itself mapped.** `dfflibmap`
  maps every flip-flop to a liberty cell *before* `abc` runs, so registers
  are outside the reported path; this is not a register-to-register signoff
  path. When Yosys invokes ABC once per combinational region, the largest
  region's reported delay is the one published here.
- **`source` names its provenance.** A later OpenSTA-backed number will
  carry a different `source`, so a caller can tell them apart without
  guessing. Never treat `critical_path_ps` as slack.

The Yosys survey section 3.5/3.6 finding it supersedes is narrower than it
looked, and still stands as written: *Yosys's own* `sta`/`ltp` passes still
cannot produce a usable report against a liberty-mapped netlist (every
mapped standard-cell type is "not recognised! Ignoring."). What changed is
that ABC prints its own number inside the subprocess this command already
runs — it was simply discarded, because `-constr` was never passed
(`docs/design/synthesize-qor-improvements-survey.md` sections 1.2/1.4).
Signoff timing is still Phase 4's OpenROAD/OpenSTA step.

`timing` is `null` — never a fabricated number — when the resolved
`cell_library` has no ABC constraint-table entry (so `-constr`, and
therefore `stime -p`, never ran), or when the resolved ABC printed no
recognisable summary line.

## `sta`: the native gate-level critical-path report

[Epic #704](https://github.com/2AMLogic/klayout-tools/issues/704) Phase 3
(issue #925) wires
[`klt-statime-native`](../../native/statime/README.md) — the native-Rust
NLDM/static-timing engine issue #809 shipped as a spike — into this command
as an **additive** stage. After `write_verilog -noattr` produces
`netlist_path`, that same file is handed to the engine along with the same
resolved liberty, and the result lands in the response's `sta` field:

```json
"sta": {
  "source": "klt_statime_native",
  "input_transition_ns": 0.05,
  "output_load_pf": 0.03,
  "top": "gcd",
  "num_cells": 353,
  "num_nets": 388,
  "worst_path": {
    "startpoint": "_640_/Q",
    "startpoint_kind": "flip-flop (rising edge)",
    "endpoint": "_035_",
    "endpoint_kind": "internal net",
    "delay_ns": 4.4642972825718,
    "hops": [
      {
        "point": "_640_/Q",
        "cell": "sky130_fd_sc_hd__dfrtp_1",
        "edge": "fall",
        "arrival_ns": 0.41273907228437867,
        "slew_ns": 0.0925558496296774
      },
      {
        "point": "_377_/Y",
        "cell": "sky130_fd_sc_hd__xnor2_1",
        "edge": "fall",
        "arrival_ns": 0.6083993722243425,
        "slew_ns": 0.11961144661859305
      }
    ]
  },
  "worst_reg_to_reg_path": {
    "startpoint": "_640_/Q",
    "startpoint_kind": "flip-flop (rising edge)",
    "endpoint": "_035_",
    "endpoint_kind": "flip-flop (D, setup)",
    "delay_ns": 4.4642972825718,
    "hops": ["...same shape, 12 hops..."]
  }
}
```

(Real output, elided only in `hops` — measured against
`tests/corpus/statime/gcd_netlist.v` + `sky130_fd_sc_hd__tt_025C_1v80`; the
full `worst_path.hops` array is 12 entries, one per cell on the path.)

**`sta` and `timing` are different numbers, and neither replaces the
other.** `timing` is ABC's own `stime -p` line (see above): pre-layout,
wire-free, and confined to the largest *combinational* cone ABC mapped, with
no path or cell breakdown available at all. `sta` is a real timing-graph
walk over the **whole** mapped netlist, rise/fall-aware, with a full per-hop
cell breakdown — `worst_path` is the globally worst path whatever its
endpoints (register-to-register, register-to-port, or port-to-port), and
`worst_reg_to_reg_path` reports the worst *pure* register-to-register path
separately (`null` for a purely combinational design). Every pre-#925
consumer of `timing` is unaffected; `sta` is a new sibling field, which
`docs/json-contract.md` treats as additive (no `schema_version` bump).

**Accuracy.** On the 3-design corpus slice
(`tests/corpus/statime/`) the engine lands within **1.34%** of an OpenSTA
oracle (`gcd` 1.34%, `mult8` 0.36%, `modexp` 0.45%), always an
*under*-estimate — verified through this integrated path, not just the
standalone binary, by `tests/test_sta_corpus.py`. See
[`native/statime/README.md`](../../native/statime/README.md) "Results —
accuracy" for how the oracle was captured.

**Read the caveats — `sta` is still not signoff STA either:**

- **No SDC, no `create_clock`.** The engine models no clock at all. Every
  primary input — *including the clock net* — gets the same uniform input
  transition, and every primary output the same uniform extra load:
  `input_transition_ns: 0.05` and `output_load_pf: 0.03`, echoed in the
  response so the boundary condition is never implicit. These are the exact
  values the accuracy comparison above ran with, matching OpenSTA's own
  `-unconstrained` report mode. They are **not** derived from the request's
  `constraints.clock_period_ns`, and **not** from the ABC `-constr` table
  that feeds `set_driving_cell`/`set_load` (a driving-*cell name* plus a
  load in femtofarads — a different knob in different units, not
  interchangeable with these two).
- **`delay_ns` is a path delay, never slack.** With no clock modeled there
  is no required time to subtract, so nothing here can tell you whether a
  design meets timing — only how long its longest path is.
- **Still wire-free.** A net's arrival equals its driver pin's arrival
  exactly; there is no placement, so no parasitics. Same estimate class as
  `timing` in this respect — a real post-layout path will be longer.
- **Register data pins are identified by the literal pin name `D`**, and
  async set/reset (`recovery`/`removal`/setup/hold) arcs are parsed but
  excluded from propagation.
- **3 corpus designs is the whole verified sample.** A design shape not yet
  seen could show a larger gap than 1.34%.

These are issue #809's own "Known simplifications" 1–5, inherited
**unresolved** — issue #925 wired the engine in unchanged rather than
hardening its numerics. Signoff timing is still Phase 4's OpenROAD/OpenSTA
step.

**`sta` is `null` — never a fabricated number — when:**

- the optional `klt_statime_native` extension is not installed (it needs a
  Rust toolchain, like every other native `klt` engine; from a checkout,
  `uv sync --group statime`, or `maturin develop --release` inside
  `native/statime/`), or
- the engine could not analyze this particular netlist/liberty pair — e.g. a
  mapped cell type with no liberty entry.

A missing extension never fails the run: synthesis itself does not depend on
this stage, so `klt synthesize` behaves exactly as it did before #925 and
simply reports `"sta": null`. This mirrors `timing`'s own "no number to
report" discipline.

## Equivalence gate

[Epic #704](https://github.com/2AMLogic/klayout-tools/issues/704) Phase 1:
`--verify-equivalence` wires [`klt equiv`](equiv.md) (#726) in as this
command's own **acceptance gate** — a synthesized netlist is not considered
done until `klt equiv` reports it `"equivalent"` to the source RTL that was
fed into Yosys. Off by default (additive/opt-in; every pre-`--verify-
equivalence` invocation is unaffected).

When given, after `synth`/`abc`/`write_verilog` produce `netlist_path`, this
command builds a `klt equiv` request reusing that command's own contract
(see [`docs/cli/equiv.md`](equiv.md)) — `gold` is the same `sources`/
`hdl_toplevel` this run just synthesized; `gate` is the just-produced
`netlist_path`, with `gate.liberty` set to the same resolved liberty this
synthesis run used (so the netlist's standard-cell instances resolve as
real combinational logic, not an undefined blackbox — see `klt equiv`'s
"Request" section) — and runs it. The generated equiv request and its own
artifacts (the `.ys` script, the flattened combined netlist, the raw Yosys
log) land under `.klt/synthesize/.klt/equiv/`, alongside this run's own
`script_path`/`netlist_path` — never deleted, kept as debuggable artifacts
like every other file this command writes. The outcome:

- **`"equivalent"`** — attached to the response's `equivalence` field (see
  "Response" below); `klt synthesize` itself still exits `0`.
- **`"counterexample"` or `"inconclusive"`** (a proven divergence or a
  solver/process timeout) — a **hard failure**: `SynthesizeError`, exit `1`,
  never a silent warning folded into a `status: "ok"` response. An
  `"inconclusive"` verdict (timeout) is never treated as a pass, mirroring
  `klt equiv`'s own "timeout is never equivalent" discipline one level up.

**Combinational designs only** — `klt equiv`'s Phase 0 MVP scope (#707) is
combinational-only; a design containing flip-flops, latches, or memories
(e.g. the GCD worked example below) makes the gate itself fail with a clear
scope error, even though synthesis succeeded. `--verify-equivalence` is not
yet usable on sequential designs — see `docs/cli/equiv.md`'s "Scope"
section and Out of scope below.

## Request

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
| `schema` | string | Request contract identifier + major version. Not validated — user-authored input, never emitted by this tool. |
| `engine` | string | `"yosys"` (default; only value implemented). |
| `sources` | array\<string\> | RTL source file paths (`read_verilog` inputs), resolved relative to the request file's own directory. Required, non-empty. |
| `hdl_toplevel` | string | The design's top module name. Required. |
| `pdk.cell_library` | string | Standard-cell library name. Required. |
| `pdk.corner` | string \| omitted | Liberty corner selector; defaults to the nominal corner when omitted. |
| `constraints.clock_period_ns` | number \| null | The target clock period in nanoseconds, consumed as ABC's own delay target: passed as `abc -D <clock_period_ns × 1000>` picoseconds, and echoed in the response as `timing.delay_target_ps`. Must be a positive number when given (a non-numeric or non-positive value is an error, never silently ignored). Yosys still has no SDC-reading step — this is the request field translated into the one delay knob the engine does expose. |

**`clock_period_ns: null` (or omitted) is a defined state, not a fallback.**
The run still passes `-constr`, so ABC's `buffer`/`upsize`/`dnsize` sizing
steps and its `stime -p` report both run and `timing` is still populated;
only `-D` is omitted, leaving ABC's mapper and sizers to optimize
untargeted exactly as they did before this field was consumed.
`timing.delay_target_ps` is `null` in that case, so a caller can always tell
a targeted run from an untargeted one. Measured effect of the target on
`gcd` (Yosys 0.68+48, sky130, issue #807): untargeted maps to 3238.11 µm² at
2485.93 ps; `clock_period_ns: 10` maps to 3001.63 µm² at 3072.40 ps — a
relaxed target buys area back at the cost of delay, so the value is a real
caller decision rather than something this command should pick.

## Response

```json
{
  "schema_version": 1,
  "engine": "yosys",
  "engine_version": "0.68+48",
  "hdl_toplevel": "gcd",
  "status": "ok",
  "instance_count": 347,
  "area_um2": 3238.1056,
  "sequential_area_um2": 1251.2,
  "instance_counts_by_type": {
    "sky130_fd_sc_hd__a211o_1": 1,
    "sky130_fd_sc_hd__dfrtp_1": 50
  },
  "timing": {
    "source": "abc_stime",
    "wire_load": null,
    "critical_path_ps": 2485.93,
    "delay_target_ps": null
  },
  "sta": {
    "source": "klt_statime_native",
    "input_transition_ns": 0.05,
    "output_load_pf": 0.03,
    "top": "gcd",
    "num_cells": 353,
    "num_nets": 388,
    "worst_path": { "startpoint": "_640_/Q", "endpoint": "_035_", "delay_ns": 4.4642972825718, "hops": ["..."] },
    "worst_reg_to_reg_path": { "...": "same shape" }
  },
  "netlist_path": "/abs/path/.klt/synthesize/gcd_synth.v",
  "script_path": "/abs/path/.klt/synthesize/synth_gcd.ys",
  "provenance": {
    "klt_version": "0.1.0",
    "klayout_version": "0.30.10",
    "pdk": { "name": "sky130A", "source": "PDK_ROOT environment variable", "version": "<stamp>" },
    "deck": { "name": "sky130_fd_sc_hd__tt_025C_1v80", "content_hash": "sha256:<hex>" },
    "input": { "content_hash": "sha256:<hex>" }
  },
  "equivalence": null
}
```

| Field | Type | Description |
| --- | --- | --- |
| `schema_version` | integer | Per-command version, per `docs/json-contract.md`. |
| `engine` / `engine_version` | string | Echo of the request's engine, plus the resolved Yosys build string (`yosys -V`'s own version token). `engine_version` is `null` if unresolvable. |
| `hdl_toplevel` | string | Echo of the request. |
| `status` | string | Always `"ok"` — synthesis has no pass/fail concept of its own; a failed run never emits this envelope. |
| `instance_count` | integer | Total standard-cell instances after liberty mapping, rolled up over the **whole design hierarchy** — `stat -json`'s per-module `num_cells` aggregated recursively across every sub-module Yosys left un-flattened, each level scaled by its instance count (issue #821; the top module's own `num_cells` alone is `0` for a design whose top is a pure wrapper). Matches `stat -json`'s own `design.num_cells` rollup. **Deliberately not named `cell_count`**: `klt layout-metrics`'s existing `cell_count` field counts *distinct cell definitions* in a GDS hierarchy, a different concept. |
| `area_um2` | number | `stat -json`'s `area`, in µm² (the liberty's own unit). |
| `sequential_area_um2` | number \| null | `stat -json`'s `sequential_area` — a floorplan hint for a future P&R step. `null` when the resolved Yosys build's `stat -json` output omits the field (distro-packaged Yosys < ~0.67, e.g. Ubuntu 24.04's 0.33 — see #560); present as a number on Yosys 0.67+. |
| `instance_counts_by_type` | object\<string, int\> | `stat -json`'s `num_cells_by_type`, rolled up over the whole hierarchy the same way `instance_count` is, keys sorted for determinism — the synthesis analogue of `klt drc`'s `rule_counts` / `klt extract`'s `device_counts`. Keys are always real leaf standard-cell types; a sub-module *name* (which `stat -json` reports as a pseudo cell type in the parent module's own block) is never reported as one, it is expanded into the cells it instantiates (issue #821). |
| `timing` | object \| null | ABC's own `stime -p` critical-path estimate: `{source, wire_load, critical_path_ps, delay_target_ps}`. `source` is `"abc_stime"`; `wire_load` is ABC's own `WireLoad` echo, `null` for its `"none"`; `critical_path_ps` is picoseconds; `delay_target_ps` echoes the `-D` value derived from `constraints.clock_period_ns` (`null` when none was given). `null` when no `stime` number is available at all. **Pre-layout and wire-free, never signoff STA** — see "`timing`" above. |
| `sta` | object \| null | `klt-statime-native`'s gate-level critical-path report over the whole mapped netlist: `{source, input_transition_ns, output_load_pf, top, num_cells, num_nets, worst_path, worst_reg_to_reg_path}`. `source` is `"klt_statime_native"`; `input_transition_ns`/`output_load_pf` echo the uniform boundary condition this run used. `worst_path` is the globally worst path — `{startpoint, startpoint_kind, endpoint, endpoint_kind, delay_ns, hops}`, where `hops` is the per-cell breakdown (`{point, cell, edge, arrival_ns, slew_ns}`) — and `worst_reg_to_reg_path` is the same shape for the worst *pure* register-to-register path (`null` for a purely combinational design). `null` when the optional `klt_statime_native` extension is not installed or the engine could not analyze this netlist/liberty pair. **A path delay, never slack, and never signoff STA** — no SDC/`create_clock`, still wire-free; see "`sta`" above. Additive as of issue #925 — `timing` is unaffected. |
| `netlist_path` | string | The mapped gate-level netlist (`write_verilog -noattr`'s output), an absolute path. Never re-derive `instance_count`/`area_um2` by parsing this file. |
| `script_path` | string | The generated `.ys` script, an absolute path — kept as a debuggable artifact. |
| `provenance` | object | The shared envelope block (`docs/json-contract.md`). `deck` names the resolved liberty file (`<cell_library>__<corner>`); `pdk` is `find_pdk()`'s resolved triple; `input` is the content hash of `sources` (a combined, order-independent hash when more than one source file is given). |
| `equivalence` | object \| null | `null` unless `--verify-equivalence` was given. When given and the gate passed: `{status: "equivalent", engine, engine_version, timeout_s, elapsed_s, artifacts}` — `artifacts` is `klt equiv`'s own `{script_path, netlist_path, log_path}` (see [`docs/cli/equiv.md`](equiv.md)). A non-equivalent or inconclusive verdict never reaches this field — it is a `SynthesizeError` instead (see "Equivalence gate" above). |

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Synthesis succeeded, netlist written (and, with `--verify-equivalence`, proven equivalent to its source RTL). |
| `1` | Failed to run — bad request, unreadable RTL source, elaboration/hierarchy error, unresolvable `pdk.cell_library`/`corner` (no matching liberty via `find_pdk()`), a Yosys/ABC engine error — or, with `--verify-equivalence`, a non-equivalent (`"counterexample"`) or inconclusive (timeout) `klt equiv` verdict against the produced netlist. |
| `2` | Usage error (missing argument, bad `--format` value) — from argparse. |

**No exit code `3`.** Matching `klt extract`'s reasoning exactly — there is
no "ran but found problems" outcome for synthesis: it either produces a
netlist or it fails. `instance_count`/`area_um2` are not a pass/fail gate
themselves — a caller wanting a threshold on them composes this contract
into `klt eval`'s descriptor, the same way `docs/cli/eval.md`'s own example
already thresholds `layout-metrics`'s `cell_count`.

## Worked example

The GCD RTL used in the Yosys survey's own worked example, synthesized
against `sky130_fd_sc_hd`'s typical corner:

```console
$ klt synthesize request.json --format json
{
  "schema_version": 1,
  "engine": "yosys",
  "engine_version": "0.68+48",
  "hdl_toplevel": "gcd",
  "status": "ok",
  "instance_count": 347,
  "area_um2": 3238.1056,
  "sequential_area_um2": 1251.2,
  "timing": {
    "source": "abc_stime",
    "wire_load": null,
    "critical_path_ps": 2485.93,
    "delay_target_ps": null
  },
  ...
}
```

347 standard-cell instances, 3238.1056 µm² total (1251.2 µm² sequential),
2485.93 ps ABC-estimated critical path, and **zero** `lpflow_*`/`probe*`
cells — measured live on Yosys 0.68+48 against a volare `sky130A` install.
Note this particular design is **sequential** (it has a clocked `always`
block) — see "Equivalence gate" above, `--verify-equivalence` is not usable
on it.

Before the ABC constraint/exclusion flags landed (issue #807), the same
request on the same toolchain mapped to **335 instances / 2951.5808 µm²**
with no delay number at all and 19 `lpflow_*` power-isolation instances in
the netlist. The ~10% area difference is the cost of ABC's
`buffer`/`upsize`/`dnsize` sizing plus the exclusion list; supplying
`constraints.clock_period_ns` gives the caller the knob to trade it back
(see the request table above). Yosys's own mapping result also moves between
releases — the same pre-#807 flow maps this design to 326 instances on
Ubuntu 24.04's Yosys 0.33 — so treat these figures as anchored to the build
named here, not as invariants.

With the optional `klt_statime_native` extension built (`uv sync --group
statime`), the same run additionally reports the native critical path.
Measured live on Yosys 0.68+post against a volare `sky130A` install
(`examples/functional-verification/gcd.v`, 402 instances / 3244.3616 µm²
on that build):

```console
$ klt synthesize request.json
...
critical_path_ps: 2584.58 (source: abc_stime, target: none, wire_load: none -- pre-layout estimate)
sta.worst_path: 2.8642 ns (_721_/Q -> _019_, source: klt_statime_native)
sta.worst_reg_to_reg_path: 2.8642 ns (_721_/Q -> _019_)
```

The two numbers **do not agree, and are not supposed to** — 2584.58 ps is
ABC's own estimate over the largest combinational cone it mapped, while
2.8642 ns is a whole-netlist register-to-register walk (13 hops, each cell
listed under `sta.worst_path.hops` in the JSON output). See "`timing`" and
"`sta`" above for what each one is and is not.

A 4-bit ripple-carry adder (`adder4.v`, combinational — no clock, so it is
in-scope for the gate — the same design [`docs/cli/equiv.md`](equiv.md)'s
own worked example uses), synthesized with the gate enabled:

```console
$ klt synthesize adder4_request.json --verify-equivalence --format json
{
  "schema_version": 1,
  "engine": "yosys",
  "hdl_toplevel": "adder4",
  "status": "ok",
  ...
  "equivalence": {
    "status": "equivalent",
    "engine": "yosys",
    "engine_version": "0.67+post",
    "timeout_s": 60.0,
    "elapsed_s": 0.08,
    "artifacts": {
      "script_path": "/abs/path/.klt/synthesize/.klt/equiv/equiv.ys",
      "netlist_path": "/abs/path/.klt/synthesize/.klt/equiv/equiv_netlist.v",
      "log_path": "/abs/path/.klt/synthesize/.klt/equiv/equiv.log"
    }
  }
}
```

A synthesized netlist that diverges from its own source RTL (e.g. a
corrupted/mis-synthesized output) makes the gate fail hard instead —
`klt synthesize` exits `1` with a message naming the diverging outputs,
never a `status: "ok"` response — see `tests/test_synthesize_equiv_gate.py`
for the full seeded-mismatch demonstration.

## Out of scope

- **Sequential-design equivalence checking.** `--verify-equivalence` only
  works on combinational designs today — `klt equiv`'s Phase 0 MVP scope
  (#707). A future phase of #707 (temporal induction / BMC via SymbiYosys)
  extends the gate to sequential designs; until then, running
  `--verify-equivalence` against a design with flip-flops, latches, or
  memories fails the gate with a clear scope error, not a misleadingly
  confident verdict.
- **Signoff timing.** Neither delay this command reports is signoff STA.
  `timing` is ABC's pre-layout, wire-free, combinational-cone-only estimate
  (see "`timing`" above); `sta` walks the whole netlist and does report
  register-to-register paths, but is still wire-free and still models no
  clock at all — a path delay, never slack (see "`sta`" above). Real STA
  (wire RCs, slack against an SDC) is Phase 4's OpenROAD/OpenSTA step.
- **Timing-*driven* synthesis.** `sta` is reported, not optimized against:
  nothing in this command feeds the critical path back into the mapper.
  That restructuring loop is issue #926's scope (Epic #704 Phase 3).
- **Place-and-route.** `netlist_path` is this command's own deliverable and
  the input to Phase 4's `klt place-and-route` (`netlist_path` becomes that
  contract's `netlist` request field) — this command does not floorplan,
  place, or route.
- **Fleet-scale evaluation of many design-space candidates.** See
  [`docs/cli/place-and-route.md`](place-and-route.md)'s "Fleet evaluation of
  digital candidates" section (Epic #391 Phase 6) — `klayout_tools.digital_fleet`
  composes this command with `klt place-and-route`/`klt eval` into one
  fleet-scheduled candidate job; this command itself takes no `--backend`/
  `--hosts` flag.
- **A second synthesis engine.** `request.engine` exists from day one so a
  later backend (e.g. a Siemens tool) is an additive enum value and a new
  glue module, never a contract-shape change — but only `"yosys"` is
  implemented today.
