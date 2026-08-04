# `klt place-and-route`

Place and route a gate-level netlist (`klt synthesize`'s own `netlist_path`
output) against a resolved sky130 standard-cell LEF/liberty deck via
OpenROAD's native Tcl API, stage by stage — Phase 4 of
[Epic #391](https://github.com/2AMLogic/klayout-tools/issues/391) ("adopt the
digital engine class — Yosys + OpenROAD — RTL→GDS as a first-class `klt`
flow").

```
klt place-and-route <request> [--format text|json]
```

This is the build phase carried by two accepted Phase 1 documents — read
them first; where this document and the code disagree with either, this
document (and the code) win:

- [`docs/design/openroad-invocation-survey.md`](../design/openroad-invocation-survey.md)
  (#397) — the invocation shape (`openroad -no_init -exit script.tcl`,
  wrapping OpenROAD's native Tcl API stage-by-stage rather than
  OpenROAD-flow-scripts' (ORFS) Makefile), the three supported floorplan
  methods, and the DEF→GDS merge approach (`def2stream.py`'s plain-`pya`
  -function structure, ported in-process onto `klayout.db`).
- [`docs/design/digital-flow-contracts-spike.md`](../design/digital-flow-contracts-spike.md)
  section 5 (#399) — the request/response JSON contract, the `target_stage`
  partial-completion design, and the exit-code table this command
  implements.

Like `klt synthesize`/`klt lvs`/`klt sim`, `klt place-and-route` takes a
**request document**, not positional file args.

- `<request>` — a path to a request JSON file. Relative paths inside the
  request (`netlist`, `floorplan.def_path`) resolve against the **request
  file's own directory**.
- `--format` — `text` (default, a human-readable summary) or `json`.

## Engine

`request.engine` is a data field, not a code path (contract spike section
2) — `"openroad"` is the only value implemented today; an unsupported value
is an application error (exit 1). OpenROAD is invoked as a **subprocess**
(`openroad -no_init -exit -metrics <file>.json <script>.tcl`), once per
stage — there is no Python binding for OpenROAD. Requires an `openroad`
binary on `$PATH`; a missing binary is a clear, actionable error (exit 1),
never a traceback.

## Stage granularity and invocation shape

The contract names exactly four stages, in execution order:
`"floorplan"` → `"place"` → `"cts"` → `"route"`. `klt place-and-route` runs
**one OpenROAD process per stage requested**, chained via `write_db`/
`read_db` ODB checkpoints between invocations — every stage's generated Tcl
script re-issues `read_liberty`/`create_clock` (OpenSTA's linked-library/SDC
state is not carried across a process boundary by `write_db`/`read_db`).
This is what makes `target_stage` a simple "how many processes to run"
decision, and what gives each `stages[]` entry its own clean
`-metrics <file>.json` snapshot.

`-metrics <file>.json` is the primary structured-output channel — confirmed
working end-to-end for issue #425's own worked example (a real
`openroad/orfs` container run against a real volare-fetched `sky130A`
install): it captures a flat JSON object of every named metric a stage's own
commands populated. The one exception: OpenROAD has no `*_metric` proc for
setup/hold timing-*violation counts* (only the scalar WNS/TNS) — those are
recovered by counting `"(VIOLATED)"` lines in
`report_check_types -violators -format end`'s own stdout, the documented
fallback.

## Floorplan methods

Three of ORFS's four floorplan-initialization methods are supported (the
fourth, IO-ring/footprint, is out of scope for a core-only block per the
survey section 2):

| `floorplan.method` | Required fields | Tcl call |
| --- | --- | --- |
| `"utilization"` (default) | `utilization_pct`, `site` (optional: `aspect_ratio`, `core_margin_um`) | `initialize_floorplan -utilization ...` |
| `"explicit"` | `die_area_um`, `core_area_um` (each `[llx, lly, urx, ury]`), `site` | `initialize_floorplan -die_area ... -core_area ...` |
| `"def"` | `def_path` | `read_def -floorplan_initialize <def_path>` |

A request naming fields from more than one method is rejected (exit 1),
mirroring ORFS's own `methods_defined > 1` check.

## PDK / LEF / liberty resolution

`request.pdk.cell_library`/`corner` resolve a liberty exactly as `klt
synthesize` already does, via `find_pdk()`/`libs_ref` discovery
(`src/klayout_tools/pdk.py`). The tech + merged-cell LEF pair resolves via
the new `klayout_tools.pdk.lef_files()` resolver (issue #397/#425 — the
survey's own finding that `_ASSET_LAYOUT` never carried a `lef` key), which
looks for open_pdks' own `<libs_ref>/<cell_library>/techlef/<cell_library>
__<corner>.tlef` (a min/nom/max routing-parasitic corner, default `"nom"`)
and `<libs_ref>/<cell_library>/lef/<cell_library>.lef`.

An unresolvable liberty or LEF is a clear **"liberty/LEF not found for
deck"** application error (exit 1), matching `klt drc`'s existing "deck
requires an asset the resolved install doesn't ship" posture.

`sky130_fd_sc_hd` is the only cell library with known CTS-buffer/routing
-layer constants today (`_CTS_BUFFER_CELLS`/`_ROUTING_LAYER_RANGE` in
`place_and_route.py`, read from ORFS's own `platforms/sky130hd/config.mk`
as reference data, never a runtime dependency) — reaching `target_stage:
"cts"` or `"route"` with a different cell library is a clear error.

## DEF→GDS merge

Ported directly from ORFS's `def2stream.py` (survey section 4) onto this
repo's `klayout.db` package, in-process — never a `klayout.sh -zz -rd ... -r
def2stream.py` subprocess. Runs only once a `"route"`-stage `write_def` has
completed; merges the routed DEF with the resolved standard-cell GDS view
(and the tech+cell LEF, for layer/pin geometry) into a single top-level-only
GDS, the same "0 missing/orphan cells" check `def2stream.py` performs.

## Request

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
| `schema` | string | Request contract identifier + major version. Not validated — user-authored input, never emitted by this tool. |
| `engine` | string | `"openroad"` (default; only value implemented). |
| `netlist` | string | The gate-level netlist path — typically `klt synthesize`'s own `netlist_path` output. Required. Resolved relative to the request file's own directory. |
| `hdl_toplevel` | string | The design's top module name. Required. |
| `pdk.cell_library` | string | Standard-cell library name. Required. |
| `pdk.corner` | string \| omitted | Liberty corner selector; defaults to the nominal corner when omitted. |
| `floorplan.method` | string | `"utilization"` \| `"explicit"` \| `"def"` — see "Floorplan methods" above. Required. |
| `io.layer_h` / `.layer_v` | string | Horizontal/vertical I/O routing layers for `place_pins`. Required once `target_stage` reaches `"place"` or later. |
| `constraints.clock_port` / `.clock_period_ns` | string / number | Clock port name + target period (ns). Required once `target_stage` reaches `"place"` or later — stages beyond floorplan have no meaning without a clock. |
| `seed` | integer | Placement/routing seed. **Required** — P&R is genuinely stochastic; a stored result must be reproducible. Echoed unchanged in the response. |
| `target_stage` | string | One of `"floorplan"`, `"place"`, `"cts"`, `"route"` (default) — how far this run is asked to go. See "Partial completion" below. |

## Response

```json
{
  "schema_version": 1,
  "engine": "openroad",
  "engine_version": "26Q3-771-g7cfb2105c9",
  "hdl_toplevel": "gcd",
  "status": "ok",
  "stage_reached": "route",
  "seed": 1,
  "die_area_um2": 8487.94,
  "core_area_um2": 7607.3,
  "utilization_pct": 44.6546,
  "wirelength_um": 9616.0,
  "worst_slack_ns": -2.18828,
  "total_negative_slack_ns": -82.8171,
  "fmax_mhz": 304.11,
  "setup_violation_count": 3,
  "hold_violation_count": 1,
  "estimated_power_mw": 11.6,
  "stages": [
    { "name": "floorplan", "die_area_um2": 8487.94, "core_area_um2": 7607.3, "utilization_pct": 38.7993, "worst_slack_ns": -3.71641, "total_negative_slack_ns": -143.072 },
    { "name": "place", "...": "..." },
    { "name": "cts", "...": "..." },
    { "name": "route", "...": "..." }
  ],
  "def_path": "/abs/path/.klt/place-and-route/gcd.def",
  "gds_path": "/abs/path/.klt/place-and-route/gcd.gds",
  "provenance": {
    "klt_version": "0.1.0",
    "klayout_version": "0.30.10",
    "pdk": { "name": "sky130A", "source": "PDK_ROOT environment variable", "version": "<stamp>" },
    "deck": { "name": "sky130_fd_sc_hd__tt_025C_1v80", "content_hash": "sha256:<hex>" },
    "input": { "content_hash": "sha256:<hex>" }
  }
}
```

| Field | Type | Description |
| --- | --- | --- |
| `schema_version` | integer | Per-command version, per `docs/json-contract.md`. |
| `engine` / `engine_version` | string | Echo of the request's engine, plus the resolved OpenROAD build string (`openroad -version`'s own token). `engine_version` is `null` if unresolvable. |
| `hdl_toplevel` | string | Echo of the request. |
| `status` | string | Always `"ok"` — place-and-route has no pass/fail concept of its own; a failed run never emits this envelope. |
| `stage_reached` | string | The last stage this run actually completed. Always equal to or beyond the request's own `target_stage` in a successful response. |
| `seed` | integer | Echo of the request. |
| `die_area_um2` / `core_area_um2` / `utilization_pct` | number | From `initialize_floorplan`/`report_design_area_metrics`, at `stage_reached`. |
| `wirelength_um` | number \| null | HPWL at `stage_reached`; `null` before placement. |
| `worst_slack_ns` / `total_negative_slack_ns` | number | WNS/TNS at `stage_reached`. Negative values are expected, not an error — a caller wanting a pass/fail gate on timing composes this contract into `klt eval`. A `target_stage: "floorplan"` request with no `constraints` (a clock is not required until `"place"`, see below) reports OpenROAD's own unconstrained-design sentinel (`1e+39`/`0`) rather than a real number — a `constraints`-less floorplan-only run has no clock to measure slack against, and this field is never fabricated to hide that. |
| `fmax_mhz` | number \| null | `null` before placement (floorplan-stage ideal-clock STA reports no `fmax`). |
| `setup_violation_count` / `hold_violation_count` | integer \| null | `null` at the floorplan stage (no placement-aware timing yet). |
| `estimated_power_mw` | number \| null | `null` before placement. |
| `stages` | array\<object\> | One entry per completed stage through `stage_reached`, each with whatever subset of the top-level metric fields that stage's own OpenROAD reports populate. The top-level fields above are always the **last** entry in `stages`, restated at top level. |
| `def_path` | string \| null | Populated once `write_def` has run (i.e. `stage_reached` is `"route"`); `null` otherwise. |
| `gds_path` | string \| null | Populated only once the DEF→GDS merge has also completed; `null` otherwise. |
| `provenance` | object | The shared envelope block (`docs/json-contract.md`). `deck` names the resolved liberty file (`<cell_library>__<corner>`); `pdk` is `find_pdk()`'s resolved triple; `input` is the content hash of `netlist`. |

## Partial completion (`target_stage`)

A request with `target_stage: "place"` asks only for floorplan through
detailed placement — a successful (`exit 0`) run of that request has
`stage_reached: "place"`, `def_path`/`gds_path` both `null` **by design**
(never requested), and every metric field populated through placement. This
is a normal, successful, partial-by-request outcome, not a degraded one.

A request whose `target_stage` (default `"route"`) the engine fails to
reach — an internal OpenROAD/CTS/routing error, or a validation failure —
is a **failed run** (exit `1`, no envelope emitted). `stage_reached` in a
successful response always equals or exceeds the request's own
`target_stage`; a response where those differ is never emitted.

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Place-and-route reached (at least) the requested `target_stage`. |
| `1` | Failed to run — bad request, unresolvable netlist/PDK/LEF, a floorplan spec with more than one method set, or an OpenROAD engine error that stops the run before reaching the requested `target_stage`. |
| `2` | Usage error (missing argument, bad `--format` value) — from argparse. |

**No exit code `3`.** Timing slack and violation counts are data, not a
built-in pass/fail gate — a negative `worst_slack_ns` is an expected,
correctly-reported number, not a contract-level failure. A caller wanting
"did timing close" as a pass/fail gate composes this contract into `klt
eval`'s descriptor with an explicit threshold, the same mechanism
`docs/cli/eval.md`'s own example already uses for `layout-metrics`'s
`cell_count`.

## Out of scope

- **Macro placement, tapcell insertion, power-grid generation (PDN), metal
  fill, `DONT_USE_CELLS`-style cell exclusion.** A core-only block v1,
  matching the contract's own IO-ring/footprint exclusion — none of these
  are part of the request/response contract this phase implements, and can
  be added later as additive request fields without a contract-shape
  change.
- **IO-ring/footprint floorplanning.** Out of scope for a core-only block,
  per the OpenROAD survey section 2.
- **A second P&R engine.** `request.engine` exists from day one so a later
  backend (e.g. a Siemens tool) is an additive enum value and a new glue
  module, never a contract-shape change — but only `"openroad"` is
  implemented today.

## See also

Running many candidate synthesis/P&R combinations across a fleet of remote
hosts — a design-space exploration over synthesis strategy, floorplan, or
P&R seed — is [`klt eval`](eval.md)'s
["Fleet-based candidate sweeps"](eval.md#fleet-based-candidate-sweeps)
section: each candidate's remote job runs this command as the final stage
of a `klt eval` descriptor's `synthesize` → `[functional-verification]` →
`place-and-route` pipeline, not this command standalone.
