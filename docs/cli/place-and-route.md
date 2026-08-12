# `klt place-and-route`

Place and route a gate-level netlist (`klt synthesize`'s own `netlist_path`
output) against a resolved standard-cell LEF/liberty deck via OpenROAD's
native Tcl API, stage by stage — Phase 4 of
[Epic #391](https://github.com/2AMLogic/klayout-tools/issues/391) ("adopt the
digital engine class — Yosys + OpenROAD — RTL→GDS as a first-class `klt`
flow").

```
klt place-and-route <request> [--pdk VARIANT] [--pdk-root ROOT] [--format text|json]
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
- `--pdk` — PDK variant to resolve (e.g. `sky130A`); overrides `$PDK`.
  Optional — omit to use `find_pdk()`'s own default search order.
- `--pdk-root` — explicit PDK install root; overrides `$PDK_ROOT` and the
  search order.
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

The `"place"` stage's `global_placement` call runs with `-routability_driven
-timing_driven` (issue #745, P&R survey §3.1 Priority 1) — both are OpenROAD
`gpl` module flags, not a new dependency or code path. `-timing_driven`
requires a linked `create_clock`, which is already in effect by this point in
every non-floorplan stage script.

The `"route"` stage runs `repair_antennas <diode_cell>` immediately after
`detailed_route`, followed by a second `detailed_route` pass (issue #759, P&R
survey §2.7/§3.3 Priority 3) — inserting a diode instance on a violating net
changes that net's own routing, so this mirrors ORFS's own
`flow/scripts/detail_route.tcl`, which re-runs `detailed_route` right after
`repair_antennas` to route/legalize each newly inserted diode. `check_antennas`
then reports the post-repair violation count (`antenna_violation_count`)
before `write_def`.

The `"place"` stage also ends with a `write_def` of its own, to
`.klt/place-and-route/<hdl_toplevel>_place.def` (issue #785). This is a
**debug artifact only** — it is deliberately *not* reported anywhere in the
response, and `def_path` still means "the routed DEF, or `null`" exactly as
before. It exists so a post-global-placement congestion pre-check (see
"Congestion pre-check" below) can read real cell/pin geometry without a
second OpenROAD round trip; the path is derivable by any consumer via
`place_and_route.placement_def_path(place_and_route.artifact_dir(request_path),
hdl_toplevel)`.

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

Neither resolver is restricted to a single PDK family — any standard-cell
library the resolved install ships `libs_ref`/LEF assets for resolves the
same way. Reaching `target_stage: "cts"` or `"route"` additionally needs a
verified entry in `_CTS_BUFFER_CELLS`/`_ROUTING_LAYER_RANGE`/
`_ANTENNA_DIODE_CELLS` (`place_and_route.py`) for that `cell_library` — a
clock-tree buffer cell name, a signal routing-layer range, and an antenna-
diode cell are not derivable from the resolved PDK install itself, so each
supported cell library needs its own verified entry (never a runtime
dependency; never guessed). Two libraries have entries today:

| `cell_library` | CTS buffer | `set_routing_layers -signal` | Antenna-diode cell |
| --- | --- | --- | --- |
| `sky130_fd_sc_hd` | `sky130_fd_sc_hd__buf_4` | `met1-met5` | `sky130_fd_sc_hd__diode_2` |
| `gf180mcu_fd_sc_mcu9t5v0` | `gf180mcu_fd_sc_mcu9t5v0__buf_4` | `Metal2-Metal5` | `gf180mcu_fd_sc_mcu9t5v0__antenna` |

A `cell_library` with no entry in any of the three tables is a clear error,
not a guess. Unlike the CTS-buffer/routing-range pair, neither ORFS platform's
own `config.mk` names an antenna-diode cell at all — ORFS's own
`detail_route.tcl` calls `repair_antennas` with no diode-cell argument,
relying on OpenROAD's null-diode "jumper only" fallback. So the antenna-diode
table's source of truth is each platform's own standard-cell LEF instead: the
one macro each platform's LEF marks `CLASS CORE/core ANTENNACELL` (see
`_ANTENNA_DIODE_CELLS`'s own docstring in `place_and_route.py` for the full
verification trail).

Both rows are read from ORFS's own platform reference data — `platforms/
sky130hd/config.mk` and `platforms/gf180/config.mk` (whose defaults
`TRACK_OPTION ?= 9t`/`POWER_OPTION ?= 5v0` resolve to exactly
`gf180mcu_fd_sc_mcu9t5v0`) — cross-checked against those platforms' own
open-source LEFs, and never a runtime dependency on an ORFS checkout. Two
asymmetries in that table are deliberate, not typos:

- **gf180mcu's routing range starts at `Metal2`, not `Metal1`**, matching
  `platforms/gf180/config.mk`'s `MIN_ROUTING_LAYER ?= Metal2`. That
  library's standard cells pin out on `Metal1` itself, so `Metal1` is left
  to pin access and intra-cell/power-rail geometry. sky130hd has no
  equivalent constraint — its cells pin out on `li1`, below `met1`
  entirely — hence `met1-met5` there.
- **The layer-name case differs** (`Metal1` vs `met1`) because each PDK's
  own LEF uses that convention; the string is passed through to OpenROAD
  verbatim.

Neither ORFS platform pins a CTS buffer of its own (`CTS_BUF_LIST` is
optional and unset in both; there is no `CTS_BUF_CELL` variable in ORFS at
all), so ORFS lets OpenROAD auto-select. `klt place-and-route` names one
explicitly instead, for a run-to-run reproducible clock tree — see
`_CTS_BUFFER_CELLS`' own docstring in `place_and_route.py` for each entry's
exact source.

## DEF→GDS merge

Ported directly from ORFS's `def2stream.py` (survey section 4) onto this
repo's `klayout.db` package, in-process — never a `klayout.sh -zz -rd ... -r
def2stream.py` subprocess. Runs only once a `"route"`-stage `write_def` has
completed; merges the routed DEF with the resolved standard-cell GDS view
(and the tech+cell LEF, for layer/pin geometry) into a single top-level-only
GDS, the same "0 missing/orphan cells" check `def2stream.py` performs.

## Hard-macro placement (`request.macros`)

Issue #438 (Epic #393 Phase 2 Capability A) turns the "macro placement" line
in "Out of scope" below into an additive request field, closing that gap
exactly the way its own note anticipated. `request.macros` is an optional
array of hard-macro instances — e.g. a LEF abstract
[`klt lef-abstract`](socket-check.md#lef-translation) emitted from an analog
block — each fixed at a caller-given location during the `"floorplan"`
stage, via OpenROAD's own `place_macro -macro_name <instance> -location {x
y} -orientation <orientation> -exact` (verified live against a real
`openroad/orfs` container's own `help place_macro` usage string; `-exact`
places at exactly the given location rather than snapping to the nearest
legal site). Never OpenROAD's automatic macro placer (`rtl_macro_placer`) —
a socket-driven macro's location is a caller decision, not something to
optimize away.

Each macro's LEF is `read_lef`'d alongside the tech/cell LEF, **before**
`read_verilog`/`link_design` — `link_design` needs every macro's physical
view already loaded to resolve the netlist's own macro instance. The RTL's
top module must therefore instantiate a module whose name matches the
macro LEF's own `MACRO` name, with a matching port list (standard
hierarchical-netlist convention); `link_design` fails with a clear OpenROAD
error otherwise.

| `macros[]` field | Type | Description |
| --- | --- | --- |
| `instance` | string | The RTL/netlist instance name this macro placement applies to. Required, and must be unique across the array. |
| `lef` | string | Path to the macro's LEF abstract (e.g. `klt lef-abstract`'s own `output`). Required; must declare exactly one `MACRO`. Resolved relative to the request file's own directory. |
| `x_um` / `y_um` | number | The macro's lower-left corner, in micrometres, in `place_macro -location`'s own coordinate space. Both required. |
| `orientation` | string | One of `R0` (default) `R90` `R180` `R270` `MX` `MY` `MXR90` `MYR90` — OpenROAD's own orientation vocabulary. |
| `gds` | string \| omitted | The macro's own GDS view. When given, merged into the final `gds_path` alongside the standard-cell GDS view (same "0 missing/orphan cells" check). When omitted, this instance's cell is expected to stay empty in the merged GDS — not an error — for a caller only after this issue's own DEF-level placement/obstruction verification. |

**Macro-pin routability cross-check (#464).** A macro LEF pin that `klt
lef-abstract` emitted with no `PORT` geometry at all (a pin whose declared
layer is not a routing-type tech-LEF layer — e.g. a device gate pin on bare
poly, see [`klt lef-abstract`'s "Pins"](lef-abstract.md#pins) section, and
that command's own `unroutable_pins[]` echo) is validated by this command
**before** OpenROAD is invoked: for each declared macro, this command reads
the macro LEF's own `PIN` blocks and, for every pin with no `PORT`, does a
best-effort scan of the netlist's own named port connections for that
instance (`.PIN(NET)`, the form every real synthesis tool emits for a
blackbox instance). A `PORT`-less pin actually wired to a non-empty net is
rejected with a specific application error (exit `1`) naming the macro
instance, pin, and net — never surfacing only as OpenROAD's opaque
`GRT-0029` several stages into a real run. A `PORT`-less pin the netlist
leaves unconnected (or never names in that instantiation's own port list)
is **not** an error — an internally-terminated node is a legitimate macro
state. The netlist scan is deliberately conservative: when the specific
`<MACRO> <instance>( ... )` instantiation cannot be confidently located
(e.g. a positional-connection instantiation, or a placeholder/non-Verilog
netlist), the cross-check is silently skipped for that macro rather than
risk a false positive or negative — OpenROAD's own `link_design` remains
the authority on whether the netlist and LEF actually agree structurally.
Discovered during Epic #393 Phase 3 (#456); see #464 for the full repro.

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
  "macros": [
    { "instance": "u_analog", "lef": "analog_block.lef", "x_um": 12.5, "y_um": 3.0, "orientation": "R0" }
  ],
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
| `macros` | array\<object\> \| omitted | Hard-macro instances to fix at a caller-given location — see "Hard-macro placement" above. `[]`/omitted when the design has none. |
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
  "antenna_violation_count": 0,
  "estimated_power_mw": 11.6,
  "clock_skew_ns": 0.0421,
  "stages": [
    { "name": "floorplan", "die_area_um2": 8487.94, "core_area_um2": 7607.3, "utilization_pct": 38.7993, "worst_slack_ns": -3.71641, "total_negative_slack_ns": -143.072 },
    { "name": "place", "...": "..." },
    { "name": "cts", "...": "..." },
    { "name": "route", "...": "..." }
  ],
  "macros": [
    { "instance": "u_analog", "lef": "/abs/path/analog_block.lef", "x_um": 12.5, "y_um": 3.0, "orientation": "R0" }
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
| `antenna_violation_count` | integer \| null | The post-repair antenna-*violating-net* count from `check_antennas`, run right after `repair_antennas`'s own reroute pass. `null` before the `"route"` stage — this is a DRC-signoff concern (`klt drc` on the merged GDS is the gate this metric tracks), not a connectivity one; `klt lvs` is unaffected by antenna repair. |
| `estimated_power_mw` | number \| null | `null` before placement. |
| `clock_skew_ns` | number \| null | Worst setup-side clock skew (`report_clock_skew_metric -setup`) across the clock tree TritonCTS built. `null` before the `"cts"` stage — no clock tree exists yet, so there is nothing to measure skew across (issue #783). |
| `stages` | array\<object\> | One entry per completed stage through `stage_reached`, each with whatever subset of the top-level metric fields that stage's own OpenROAD reports populate. The top-level fields above are always the **last** entry in `stages`, restated at top level. |
| `macros` | array\<object\> | Echo of the request's `macros[]` (`instance`/`lef`/`x_um`/`y_um`/`orientation`; `lef` resolved to an absolute path). `[]` when the request declared none. |
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

- **Tapcell insertion, power-grid generation (PDN), metal fill,
  `DONT_USE_CELLS`-style cell exclusion.** A core-only block v1, matching
  the contract's own IO-ring/footprint exclusion — none of these are part
  of the request/response contract this phase implements, and can be added
  later as additive request fields without a contract-shape change.
  (Hard-macro placement was originally scoped out here too — see
  "Hard-macro placement" above; issue #438 closed that gap.)
- **IO-ring/footprint floorplanning.** Out of scope for a core-only block,
  per the OpenROAD survey section 2.
- **A second P&R engine.** `request.engine` exists from day one so a later
  backend (e.g. a Siemens tool) is an additive enum value and a new glue
  module, never a contract-shape change — but only `"openroad"` is
  implemented today.

## Fleet evaluation of digital candidates (Epic #391 Phase 6)

`klt sim`'s `remote`/fleet-sharding backend (see [`docs/cli/sim.md`](sim.md)'s
"Remote backend"/"Fleet sharding" sections, Epic #375) provisions and
schedules a **corner-shaped** unit of work — many lightweight ngspice runs
packed several per host. A digital design-space-exploration run wants the
opposite shape: **one candidate evaluation** (one complete
synthesis→[functional-verification]→place-and-route pipeline run at one
design-space point — a synthesis-strategy / floorplan / P&R-seed
combination) wanting ~1 whole host, never several packed per host. Rather
than build a second scheduler, Epic #391 Phase 6
([#445](https://github.com/2AMLogic/klayout-tools/issues/445), decision
record
[`docs/design/digital-fleet-unit-abstraction-decision.md`](../design/digital-fleet-unit-abstraction-decision.md))
extends #375's own fleet scheduler (`remote_fleet.py`/`remote_launcher.py`/
`remote_transport.py`, all **unchanged**) with two new pieces, living beside
it in `src/klayout_tools/digital_fleet.py`:

- **Digital-specific instance sizing**
  (`digital_fleet.select_digital_instance_type`/`digital_fleet_sizing`) — a
  fixed/configurable instance tier per PDK (or an explicit design-size-proxy
  override), never the corner-shaped `unit_count * threads_per_corner`
  formula `klt sim`'s fleet uses. `digital_fleet_sizing(hosts=..., pdk=...)`
  returns a `(shard_unit_counts, threads_per_corner)` pair that plugs
  straight into `remote_fleet.FleetLauncher`/`run_fleet`'s own **unmodified**
  constructor arguments — `shard_unit_counts` is always `[1] * hosts` (`m ~=
  1`: each shard is sized for one candidate's own compute footprint, however
  many candidates it ends up evaluating serially).
- **A digital-specific `JobDescription` builder + candidate-ranking merge
  step** (`digital_fleet.build_digital_job_description`,
  `digital_fleet.merge_candidate_results`/`rank_candidates`) — the same role
  `sim.py`'s `_build_remote_job_description` plays for `klt sim`'s own fleet
  use. One `DigitalCandidate` (RTL sources, `hdl_toplevel`, `pdk`,
  `floorplan`, `seed`, and an optional `DigitalVerification` step) becomes
  one `remote_transport.JobDescription` that uploads the RTL (+ testbench,
  when given) and runs `klt synthesize` → [`klt functional-verification`] →
  `klt place-and-route`, composed as a single `klt eval` descriptor (see
  [`docs/cli/eval.md`](eval.md), issue #387) so the pipeline's
  gate/objective/metrics envelope is produced by the existing `eval.py`
  orchestration rather than reimplemented here. `merge_candidate_results`
  flattens every shard's `ShardOutcome` into one `CandidateResult` per
  candidate (an errored/lost shard reports every candidate it was assigned
  as `status="error"`, mirroring `klt sim`'s own lost-shard convention), and
  `rank_candidates` orders the scoreable ones best-first by their declared
  `objective.polarity` — unscoreable candidates sort last, never dropped.

`digital_fleet.make_digital_shard_runner` builds a concrete
`remote_fleet.ShardRunner` (`(shard_index, launcher, public_ip) -> Any`)
from a list of candidates per shard, using the same push/run/pull/cleanup
transport `sim.py`'s single-host `_run_remote` uses — proving the sizing
function and the `JobDescription` builder genuinely compose with
`remote_fleet.run_fleet`'s existing contract, not just in theory:

```python
from klayout_tools import digital_fleet as df
from klayout_tools import remote_fleet as rf

candidates_by_shard = [[candidate_a], [candidate_b], [candidate_c]]
shard_unit_counts, threads_per_corner = df.digital_fleet_sizing(
    hosts=len(candidates_by_shard), pdk="sky130A"
)
shard_runner = df.make_digital_shard_runner(
    candidates_by_shard, ssh_user="ubuntu", ssh_key_path="~/.ssh/my-key.pem"
)

fleet_result = rf.run_fleet(
    region="us-east-1",
    pdk="sky130A",
    shard_unit_counts=shard_unit_counts,
    threads_per_corner=threads_per_corner,
    shard_runner=shard_runner,
    launcher_cidr="203.0.113.4/32",
    key_name="my-ec2-keypair",
)

results = df.merge_candidate_results(fleet_result, candidates_by_shard)
ranked = df.rank_candidates(results)  # best candidate first
```

**No `klt` CLI verb yet.** This is a library-level extension of the fleet
scheduler, not a new `--backend remote`/`--hosts` flag on `klt synthesize`/
`klt place-and-route` (neither accepts one today) — an orchestrator (a
design-space-exploration loop, or a future CLI verb) drives `digital_fleet`
directly, the way the snippet above does.

## Congestion pre-check (research spike, not wired in)

`klayout_tools.congestion` estimates post-placement routing congestion from
the `place` stage's `<hdl_toplevel>_place.def` debug artifact: per-net
rectilinear Steiner trees (Prim RMST + Kahng & Robins Iterated 1-Steiner),
RUDY bounding-box demand spreading onto a GCell-sized bin grid, and per-bin
demand-vs-track-supply utilisation statistics. It exists to answer P&R
survey §3.6's question — can a cheap estimator reject an obviously-bad DSE
candidate before paying for `global_route`/`detailed_route`?

**It is deliberately not wired into anything.** No `klt` verb calls it, and
`digital_fleet`'s candidate loop does not gate on it. Issue #785's own
go/no-go criterion was "correlate against OpenROAD's post-route
DRC-violation count, and ship only if that correlation rejects genuinely-bad
candidates without false-rejecting good ones". On the corpus slice that
criterion is measurable over, **every** candidate OpenROAD's placer accepts
routes DRC-clean (placement itself fails with `DPL-0038` above ~70%
utilisation), so the DRC oracle is constant and the correlation is
undefined. What the estimator does track well is routing *cost*, not routing
*failure* — see the PR for issue #785 for the full measurement table.

Use it directly if you want that signal:

```python
from klayout_tools import congestion, place_and_route

artifacts = place_and_route.artifact_dir("request.json")
estimate = congestion.estimate_congestion_from_def(
    place_and_route.placement_def_path(artifacts, "gcd"),
    tech_lef=".../sky130_fd_sc_hd__nom.tlef",
    cell_lefs=[".../sky130_fd_sc_hd.lef"],
    routing_layers=["met1", "met2", "met3", "met4", "met5"],
)
estimate["congestion_score"]  # p95 bin utilisation
```

`scripts/congestion-precheck-study.sh` (real OpenROAD, via
`openroad/orfs:latest`) plus `scripts/congestion_precheck_study.py`
(correlation + gate-savings model) re-run the whole study on any candidate
set, so the go/no-go verdict above is re-derivable rather than asserted.
