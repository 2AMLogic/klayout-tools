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

## Installing OpenROAD

There is no `apt`/`brew`/`pip` package for `openroad` — the only broadly
available, reproducible distribution today is the official `openroad/orfs`
Docker image (it bundles the full OpenROAD-flow-scripts (ORFS) tree, not a
documented standalone CLI install target). This section names one concrete,
copy-pasteable path to get a plain `openroad` binary onto `$PATH`.

### Docker: extract the binary onto `$PATH`

```
docker pull --platform linux/amd64 openroad/orfs:latest
```

This is the same image
[`docs/design/openroad-invocation-survey.md`](../design/openroad-invocation-survey.md)'s
own "Environment limitation" section already pulls for exactly this reason,
and the container every "verified live against a real `openroad/orfs`
container" provenance note elsewhere in this file cites. The binary is not
on the image's own `$PATH` under the short name `openroad` — as of this
image's `latest` tag digest
`sha256:0586b21f8cd1ef743f94ed85b48e4985cde7a4c90087cb5c9a7b78c8dde19903`
(OpenROAD `26Q3-1278-g4421880472`, checked live 2026-08-17), it lives at
`/OpenROAD-flow-scripts/tools/install/OpenROAD/bin/openroad`. Both the
digest and that path are a snapshot, not a stable API — they can drift when
upstream restructures the image, so re-locate the binary yourself if the
recipe below stops working:

```
docker run --rm --platform linux/amd64 openroad/orfs:latest \
  bash -lc "find / -iname openroad -type f"
```

The simplest way to get a plain `openroad` on the **host's** `$PATH` is a
one-line wrapper script that runs the container per invocation:

```bash
cat > /usr/local/bin/openroad <<'WRAPPER'
#!/usr/bin/env bash
exec docker run --rm -i \
  --platform linux/amd64 \
  -v "$PWD":"$PWD" -w "$PWD" \
  openroad/orfs:latest \
  /OpenROAD-flow-scripts/tools/install/OpenROAD/bin/openroad "$@"
WRAPPER
chmod +x /usr/local/bin/openroad
```

`-v "$PWD":"$PWD" -w "$PWD"` mounts and enters the caller's current
directory inside the container **at the identical path** — `klt
place-and-route` generates a Tcl script and writes every stage's `-metrics
<file>.json`/ODB checkpoint/DEF/GDS artifact to an absolute host path, so
the container must resolve that same absolute path, not a relocated mount
point.

Verified live (2026-08-17): with this wrapper first on `$PATH`, `openroad
-version` reports `26Q3-1278-g4421880472` — the exact version cited above,
confirming the wrapper actually reaches the in-container binary rather than
silently no-oping.

Depending on your Docker setup you may need to adjust the `docker run`
invocation itself — e.g. `sudo docker run ...` if your user isn't in the
`docker` group. Docker Desktop's `linux/amd64` emulation on a non-x86_64
host (e.g. Apple Silicon) works but is slower, per the survey's own
"Environment limitation" section, which hit the identical constraint.

### From source

No from-source build recipe is documented here yet. This is a real parity
gap against [`klt synthesize`'s CI story](synthesize.md), which installs a
pinned, checksum-verified Yosys via `scripts/install-yosys.sh` rather than
depending on Docker at all — OpenROAD's own build (CMake plus a large
C++ dependency tree: OpenSTA, OpenDB, TritonRoute, and more) has not been
pinned and verified for this repo, so there is no equivalent
`scripts/install-openroad.sh` today.

### CI

No CI job in this repo provisions `openroad` today — `.github/workflows/ci.yml`
installs `ngspice`, a pinned Yosys, and pinned Icarus Verilog/Verilator by
name, but has no step that installs OpenROAD, so `klt place-and-route` is
not yet exercised end-to-end in CI (unlike `klt synthesize`, `klt equiv`,
and `klt functional-verification`). The Docker recipe above is the
straightforward way to close that gap — a `docker run` step per invocation,
or the wrapper script above committed to the repo and put on `$PATH` before
any `klt place-and-route` call — it just is not wired up yet.

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

## Timing-driven/repair-iteration routing flags (`route_critical_nets_percentage`, `max_antenna_repair_iterations`)

Issue #939 (Epic #700 Phase 2's native-routing survey, `docs/design/
native-routing-survey.md` §4.1) audited `detailed_route`/`global_route`'s
own flag surface for a timing-driven or congestion-tuning mode not already
passed, and `repair_antennas`'s optional iteration count as a bounded
multi-pass alternative to the single-pass reroute #759 shipped.

**Methodology note.** Unlike issue #783's `clock_tree_synthesis` audit
(live `info body`/`help` introspection against a real `openroad/orfs`
container), no such container was reachable in this task's environment.
This audit instead reads OpenROAD's/OpenROAD-flow-scripts' own upstream Tcl
and C++ source directly (`The-OpenROAD-Project/OpenROAD`@`9b2de5c`/
`The-OpenROAD-Project/OpenROAD-flow-scripts`@`ef52564`, `master`, fetched
2026-08-13) — real and citable, but not independently container-verified,
and not cross-checked against the exact pinned build #783 used
(`26Q3-1080-gab6fd26351`). Both new fields below default to reproducing
today's exact behaviour for this reason, and neither was evaluated with a
real A/B run against actual OpenROAD (also unavailable in this task's
environment) — see `place_and_route.py`'s own module docstring for the full
citations and findings.

- **`global_route`** does carry a real timing-aware congestion knob,
  `-critical_nets_percentage <percent>` — the percentage of worst-slack
  nets given routing preference during congestion-removal iterations,
  force-reset to `0` internally whenever no timing data is loaded (never a
  silently-wrong result). Exposed as the optional
  `route_critical_nets_percentage` request field.
- **`detailed_route`** has **no** timing-driven or congestion-tuning flag
  at all — a genuine, cited negative result (every flag in its
  `sta::define_cmd_args` block was checked; none are timing-related).
- **`repair_antennas`** does carry a real `-iterations` flag, but OpenROAD's
  own source explicitly warns against using it once `detailed_route` has
  already run — exactly this stage's own call pattern — so a bounded
  multi-pass option is built at the **flow level** instead (mirroring
  OpenROAD-flow-scripts' own `MAX_REPAIR_ANTENNAS_ITER_DRT` loop shape):
  the optional `max_antenna_repair_iterations` request field repeats the
  `"route"` stage's `repair_antennas`/`detailed_route` reroute pair that
  many times, unconditionally (no Tcl-level early exit on a zero-violation
  `check_antennas` result).

## Multi-corner setup/hold sweep (`worst_setup_slack_ns`, `worst_hold_slack_ns`)

Issue #949 (Epic #700 Phase 3's post-route-STA survey, `docs/design/
post-route-sta-survey.md` §4.2) closed two gaps `docs/design/
post-route-sta-survey.md` §1.2 documented: the `"route"` stage's OpenSTA
session resolved exactly one PDK corner (the nominal, typical-process/
room-temperature pick), and the response had no hold-slack **value** at
all (only `hold_violation_count`, a count).

Once the `"route"` stage's own script (single-corner, unchanged) has
written its checkpoint, a **second** OpenROAD invocation reads that
checkpoint back, `define_corners`s every `.lib` timing corner the resolved
`cell_library` ships (`klayout_tools.pdk.list_lib_corners`, an additive
enumeration alongside the existing single-corner `_nominal_supply` pick),
`read_liberty -corner`s each, and re-estimates parasitics
(`estimate_parasitics -global_routing`, the same estimate the single-corner
`worst_slack_ns` is already based on) before calling
`report_worst_slack_metric -setup`/`-hold`. OpenSTA reports the worst value
across every **loaded** corner — confirmed live (against a real
`openroad/orfs:latest` container over a real volare-fetched `sky130A`
install, 2026-08-13) that this automatically worst-cases setup at the
slowest loaded corner and hold at the fastest, the standard "setup at slow
PVT, hold at fast PVT" sign-off convention — no manual slow/fast corner
classification is needed on the Python side.

- **A separate session, not more Tcl in the route stage's own script.**
  `define_corners` must run before any `read_liberty` in a session
  (OpenSTA's own `STA-482` ordering rule) — but more importantly,
  `report_worst_slack_metric` has no way to scope its result back to one
  corner once more than one is loaded (live-verified), so folding the sweep
  into the route stage's own session would silently turn the existing
  `worst_slack_ns`/`total_negative_slack_ns`/`setup_violation_count`/
  `hold_violation_count` fields from nominal-corner-only into swept-worst-
  case — breaking the backward-compatibility `docs/json-contract.md`'s
  additive posture requires. A wholly separate OpenROAD process avoids that
  risk, at the cost of a second engine invocation per `"route"`-stage run.
- **A `_ccsnoise`-suffixed `.lib` view is excluded from the sweep.** sky130
  ships a CCS-noise-model view alongside some corners' regular timing view
  (e.g. `sky130_fd_sc_hd__ff_n40C_1v95_ccsnoise.lib`) that shares its
  sibling's exact PVT point — loading both raises `[WARNING STA-1140] ...
  library <name> already exists` (live-verified) rather than adding a real
  second corner, so `list_lib_corners` skips it.
- **Wall-clock cost, measured.** The sweep is a second, full OpenROAD
  subprocess launch (parasitics re-estimation plus one `read_liberty` per
  shipped corner) on top of the four stage invocations `target_stage:
  "route"` already runs — a real, non-zero addition to total run time, not
  assumed free. Measured against a real `openroad/orfs:latest` container +
  volare `sky130A` install (`sky130_fd_sc_hd`, all 16 non-`_ccsnoise`
  corners) on the `gcd`/`modexp`/`mult8` corpus (issue #949): the sweep
  itself adds **~5–6 seconds** per run (the gap between the `"route"`
  stage's own `-metrics` write and the sweep's own), a small, roughly
  design-size-independent fixed cost relative to the `"route"` stage's own
  100+ second detailed-routing time in this corpus.
- **Corner-to-corner spread, measured.** On the same corpus, the sweep's
  worst-case setup slack is substantially worse than the single nominal
  corner's own `worst_slack_ns` — `gcd`: `-1.94402` ns (nominal) vs.
  `-22.2093` ns (swept); `modexp`: `2.42586` ns (nominal, timing-clean at
  the nominal corner) vs. `-36.479` ns (swept, timing-**failing** once the
  slowest process corner is considered) — exactly the false-positive T1
  item 5 exists to catch. Hold slack stays positive (no hold violations) at
  both nominal and swept worst-case on this corpus (`gcd`: `0.28443` ns
  swept; `modexp`: `0.22504` ns swept). `mult8`'s clock port is an
  unconstrained output pin (`tests/corpus/place_and_route/regenerate.sh`'s
  own documented fixture choice) — `worst_slack_ns`,
  `worst_setup_slack_ns`, and `worst_hold_slack_ns` all correctly report
  OpenSTA's own unconstrained-design sentinel (`1e+39`) identically, a real
  regression check that the sweep does not fabricate a number where none
  exists.
- **Only runs for the `"route"` stage.** A `target_stage` before `"route"`
  never reaches the sweep — `worst_setup_slack_ns`/`worst_hold_slack_ns`
  are `null`, the same convention `route_drc_violation_count` already
  follows for a route-stage-only metric.

## Post-route SPEF STA (`post_route_spef`, issue #948)

Issue #948 (Epic #700 Phase 3's
[post-route STA survey](../design/post-route-sta-survey.md) §4.1) closes the
gap that section's own §1.2 documents: today's `"route"`-stage timing
(`worst_slack_ns`/`total_negative_slack_ns`/violation counts, the top-level
fields below) is computed from `estimate_parasitics -global_routing` — a
coarse Elmore-style RC estimate over the global-routing Steiner topology, not
parasitics extracted from the actual detailed-routed geometry `write_def`
commits to disk.

The optional boolean `post_route_spef` request field (default `false`) opts
in to a real-parasitics **A/B** pass on top of that existing behaviour, never
a replacement of it:

1. Once the `"route"` stage's DEF→GDS merge completes, `klt extract
   --parasitics` runs against the merged routed GDS (the same first-order
   lumped-RC engine [`docs/cli/extract.md`](extract.md) documents), and its
   per-net R/C model is written as SPEF via `--spef`. That extraction also
   passes `--def-net-names` (issue #951), so each routed net is named from
   the DEF net name KLayout's LEF/DEF reader left on its geometry rather
   than from GDS text labels — which the merge emits for top-level pins
   only. This is what makes the SPEF's `*D_NET` names the same strings the
   OpenSTA session in step 2 has linked.
2. A **second** `openroad` invocation, seeded from the `"route"` stage's own
   ODB checkpoint (`read_db`), loads that SPEF via `read_spef` and re-runs
   the identical `report_worst_slack_metric`/`report_tns_metric`/
   `report_check_types` calls the primary `"route"`-stage script itself
   uses — so `spef_sta`'s own `worst_slack_ns`/`total_negative_slack_ns`/
   `setup_violation_count`/`hold_violation_count` are directly comparable to
   the top-level fields on the identical routed design, differing only in
   parasitics source.
3. **Net-name correlation is explicitly checked, not assumed** (the survey's
   own flagged open risk: does `klt extract`'s net naming correlate with
   OpenSTA's own flat linked-design net list?) — before `read_spef` runs,
   the check is measured in **both directions** against the design already
   loaded from the checkpoint:

   - `spef_sta.nets_annotated` / `.nets_total` — SPEF-side: how many of the
     names the SPEF declares exist in the design (`get_nets -quiet` per
     name). Extraction is flat, so the SPEF also carries every standard
     cell's own internal nodes, which a gate-level linked design has no
     counterpart for; **this ratio cannot reach 1 by construction** and is
     reported for completeness, not as a gate.
   - `spef_sta.design_nets_annotated` / `.design_nets_total` — design-side:
     how many of the nets OpenSTA actually times the SPEF names at all.
     **This** is the ratio that says whether the SPEF is wired into that
     session, and the one `annotation_complete` is keyed on.

   A shortfall is spelled out in words in `spef_sta.annotation_warning`
   (`null`, with `annotation_complete: true`, only on a full design-side
   match). A caller comparing `spef_sta`'s timing numbers against the
   top-level ones **must** check the design-side pair first.

### Net-name correlation: measured, and it is now complete

The survey's flagged risk materialised when `post_route_spef` first shipped
(issue #948): correlation measured **0 of 981 / 1904 / 449** nets on
`gcd`/`modexp`/`mult8`. Issue #951 closed it. Re-measured live against
`openroad/orfs:latest` (OpenROAD `26Q3-1080-gab6fd26351`), sky130A,
`seed: 1`, `target_stage: "route"`, on all three corpus proxy designs:

| design | `design_nets_annotated` / `design_nets_total` | `nets_annotated` / `nets_total` |
|---|---|---|
| `gcd` | **537 / 537** (100%) | 537 / 1356 |
| `modexp` | **760 / 764** (99.5%) | 760 / 2458 |
| `mult8` | **276 / 276** (100%) | 276 / 655 |

**What was wrong**: `klt extract` derived every net name from GDS *text
labels* ([`docs/cli/extract.md`](extract.md)'s "Coverage"), and the
`"route"` stage's DEF→GDS merge emits labels for **top-level pins only** (52
texts on `met2.LABEL` for `gcd`). Every internal routed net therefore
reached the SPEF under a name KLayout synthesised for an unlabelled net
(`$1009`, …) or built by joining the standard-cell pin labels that happen to
touch it (`A,X`) — neither of which is what OpenSTA calls that net in the
linked design (`_019_`, `req_msg[3]`, …).

**What fixed it**: the names were in the routed GDS all along, just not as
labels. KLayout's LEF/DEF reader records each routed-net shape's DEF net
name as a **GDS shape property** (`net_property_name`, default `1`), and
GDS `PROPATTR`/`PROPVALUE` round-trips it. The extraction behind
`post_route_spef` now passes `--def-net-names`
([`docs/cli/extract.md`](extract.md)) to read it. **No routed-GDS artifact
change and no corpus fixture regeneration were needed** — the committed
`tests/corpus/place_and_route/gcd.gds.gz` already carried all 458 of that
design's DEF net names, which is what
`tests/test_extract.py::test_def_net_names_recovers_the_routed_corpus_designs_own_net_names`
asserts.

**Why the two ratios differ so much.** Extraction is flat, so the SPEF also
carries every standard cell's *internal* nodes (819 of `gcd`'s 1356
`*D_NET` blocks). A gate-level linked design has no counterpart for those
by construction, so the SPEF-side ratio cannot reach 1 and says nothing
about annotation quality; the design-side ratio is the one that does.

**`modexp`'s 4 unmatched nets are correct behaviour, not a gap.** They are
`_0583_`…`_0586_` — tie-cell (`conb_1`) `LO` outputs whose DEF entry is
`- _0583_ ( _1300_ LO ) + USE SIGNAL ;` with **no `ROUTED` geometry at
all**. A net with no wire has no shape to carry a net-name property, and
equally has no interconnect parasitics to report. `annotation_complete` is
`false` for that run, correctly and conservatively.

### `*PORTS` lists only real design ports (issue #961 defect 1, fixed)

`--def-net-names` (previous section) gives *every* routed net a real name —
not just top-level pins, unlike the text-label-only naming it replaces. `klt
extract`'s own `Netlist.make_top_level_pins()` promotes *every* named net to
a top-level circuit pin with no concept of "design port" beyond "has a
name," so left unguarded, the written SPEF's `*PORTS`/`*P` list wrongly
declared every routed net a top-level port instead of just the design's
actual I/O (issue #961's own repro: `*P _019_ B` for an ordinary internal
net).

`_post_route_spef_metrics` now parses the routed DEF's own `PINS`
section — the DEF's own declaration of which nets are genuine
design-boundary ports, independent of the `--def-net-names` renaming above —
and passes that set to `klt extract` as `declared_pins` (the pre-existing
`--pins` mechanism, issue #514): every promoted pin *not* in the declared
set is demoted back to an internal net, so `*PORTS`/`*P` lists only real
top-level design ports. A DEF with no parseable `PINS` section (not expected
for a real routed DEF) falls back to the pre-#961 behaviour rather than
wrongly declaring zero ports — a routed design always has at least the
required `constraints.clock_port`, so an empty parse result is a parse
failure, never a real portless design.

Measured on the committed routed corpus fixture
(`tests/corpus/place_and_route/gcd.gds.gz`, extracted with `--def-net-names
--parasitics --spef` exactly as `post_route_spef` does):

| | `*PORTS` entries | `*D_NET` blocks |
|---|---|---|
| before (every named net promoted) | **463** | 1392 |
| after (DEF `PINS`-derived `declared_pins`) | **54** — `gcd`'s 52 I/O + `VPWR`/`VGND` | 1392 |

The `*D_NET` set is bit-for-bit unchanged, which is the point: the demotion
changes which nets are *declared ports*, never which nets carry parasitics,
so the 537 / 537 net-name annotation ratio above is untouched.
`tests/test_extract.py::test_declared_pins_restricts_spef_ports_on_the_routed_corpus`
asserts both columns.

### `*CONN` device-terminal pin correlation (issue #961, root-cause topology fix and residual fix both landed and live-verified)

Correct net *names* and a correct `*PORTS` list are necessary but not
sufficient, and the finding below was the last **live-measured** state
before this section's own fix landed. With 537 of 537 `gcd` nets matched,
`read_spef` found every net — and then rejected every node inside them:

```
[WARNING STA-1656] gcd_route.spef line 108313, pin _031___t0 not found.
...
report_parasitic_annotation
  Found 3 unannotated drivers.
  Found 537 partially unannotated drivers.
```

`klt extract`'s SPEF omitted device-terminal (`*I <instance>:<pin>`)
connectivity by design (issue #948's own original scope — see
[`docs/cli/extract.md`](extract.md)'s "SPEF export"), because the terminals
it *does* know are **transistor** terminals under this repo's own
layout-driven device naming (`$1517:G`), not the standard-cell instance pins
the linked design is built from. The `*CAP`/`*RES` node names it emitted
(`_031___t0`, …) therefore resolved to no pin in the design, so OpenSTA had
nowhere to attach the RC network: the parasitics were read, matched by name,
and then not used. Confirmed directly — `worst_slack` was bit-identical
before and after `read_spef` in the same session
(`-1.7253174444675778e-9` both times on `gcd`). The `*PORTS` fix above did
not change this — it corrects which nets are declared ports, not whether
OpenSTA can attach an RC network to the nodes inside any of them.

**What issue #961 added**: `_post_route_spef_metrics` now parses the routed
DEF's own `NETS` section (`extract.def_net_instance_pins`) for each net's
real `(instance, pin)` connections — the same instance/pin spelling the
linked gate-level design already uses, since DEF preserves the flow's
original names verbatim — and passes that mapping to `klt extract` as
`def_net_connections`. The SPEF writer emits one `*I <inst>:<pin> B` `*CONN`
entry per connection and wires it into the RC network with a zero-ohm
`*RES` leg from the net's own primary node; see
[`docs/cli/extract.md`](extract.md)'s "SPEF export" section for the exact
shape and the duplicate-net-name guard (a name shared by several un-strapped
islands, e.g. `gcd`'s 105 `VGND` islands, is skipped rather than asserting
connectivity no single island actually has). This does not model resistance
*between* DEF-level pins — this repo's own extracted parasitics have no
notion of which pin drives a net or the physical wire path to each load —
only that each real design pin sits at the net's own lumped potential, which
is what should let OpenSTA actually attach a net's capacitance to a real
driver/load pin instead of discarding the `*D_NET` block outright.

**This was verified structurally, not live, when it landed.**
`tests/test_extract.py` asserts the exact written `*CONN`/`*RES` text for
both a plain case and the duplicate-net-name skip, and defect 2 (coupling
`*CAP` entries referencing the coupled net's own hub node rather than its
bare name) is fixed and tested the same way — but no running Docker daemon
was available while implementing it, so the
`report_parasitic_annotation`/`worst_slack` before/after measurement above
had **not** been re-run against a real OpenSTA session at merge time.

**Re-measured live** (2026-08-14, `openroad/orfs:latest` OpenROAD
`26Q3-1080-gab6fd26351`, sky130A, `gcd`, `seed: 1`, fresh
`klt synthesize` → `klt place-and-route --post_route_spef` run, not the
committed corpus fixture): `report_parasitic_annotation` in the same
session that ran `read_spef` against the freshly written SPEF reports

```
Found 3 unannotated drivers.
Found 533 partially unannotated drivers.
```

— a marginal improvement over the pre-`*CONN`-correlation baseline above
(537 partially unannotated) but **not** the "no partially unannotated
drivers" the acceptance criteria require: 533 of `gcd`'s 537 annotated
drivers are still only partially wired to their RC network.

**Root cause, isolated live**: the warnings are `[WARNING STA-1656] ...
pin <net-name> not found`, and the unresolved name is consistently a
**bare net name** — including, in a minimal 2-net reproduction built
directly from `gcd_route.spef`'s own text, the *referencing* net's own
name (e.g. net `_031_`'s own `*RES 1 _031_ _704_:D 0.000000` leg warns
`pin _031_ not found`, with `_031_` being that same `*D_NET`'s own header
name, defined on the very next line up — this is not a forward-reference
ordering problem; reordering the two `*D_NET` blocks in the reproduction
made no difference). OpenSTA's SPEF reader accepts a bare net name as a
**single-node** self-capacitance-to-ground entry (`1 _031_ 2.247894`, no
warning) but does **not** accept it as one of the two endpoints of a
**two-node** `*RES` or coupling `*CAP` entry — only identifiers already
introduced via that net's own `*CONN` section (`*I <inst>:<pin>` /
`*P <port>`) resolve as a "pin" there. This repo's SPEF writer emits
exactly that invalid shape: every zero-ohm `*RES` leg (`*RES 1 <net>
<inst>:<pin> 0.000000`) and every coupling `*CAP` entry (`<n> <net>
<other-net> <val>`) uses the **bare net name** as one endpoint, standing
in for "the net's own lumped node" — a convention this repo's writer
invented and unit-tests assert, but that OpenSTA's own `read_spef` does
not recognize outside the single-node self-capacitance case.

**The fix, landed and live-verified (2026-08-14).** `_write_spef` no longer
uses a bare net name as either endpoint of any two-terminal `*RES`/coupling
`*CAP` entry. Every `*D_NET` block now plans two internal, properly-scoped
SPEF nodes (`extract._spef_net_topology_nodes`): `net_node` (`<net>:1`,
always — never the bare net or port name, even for a declared port) is
where every device-terminal leg, DEF-instance-pin (`*I <inst>:<pin>`) leg,
and the no-device-terminal Gamma-shunt resistor originate; `hub_node` is a
second node (`<net>:2`) only in that Gamma-shunt case, otherwise it is
simply `net_node`. Every self- and coupling-`*CAP` entry attaches at
`hub_node`, and a coupling entry's *other* net is referenced by *that* net's
own `hub_node` (never its bare name) via a `net -> hub_node` lookup built
up front. A `*P <port> B` `*CONN` line is still emitted for a declared port
— that block-level "this net is also a port" association needs no separate
resistor tying the bare port name into the internal RC network (see the
live finding below for why attempting one is actively harmful).

**This reworked topology was arrived at through six isolated live
reproductions**, built directly against a fresh `gcd_route.spef` and a real
OpenSTA session (`read_db`/`read_liberty`/`create_clock`/`read_spef`,
`openroad/orfs:latest`, sky130A, `gcd`, `seed: 1`), each testing one
specific node-naming hypothesis:

- A same-block `*RES` leg between a `<net>:<N>` internal node and a real
  `*I <inst>:<pin>` `*CONN` entry resolves cleanly (no warning, that one
  net fully annotated).
- A **cross-block** coupling `*CAP` entry referencing *another* net's own
  `<net>:<N>` internal node **also** resolves cleanly — internal SPEF nodes
  are visible across `*D_NET` blocks by net name, contrary to this
  section's own earlier (incorrect) assumption.
- A bare, single-token identifier — even the block's own `*P`-declared port
  name, referenced from *within that same block* — is **never** a valid
  two-node `*RES`/`*CAP` endpoint (`[WARNING STA-1656] ... pin <port> not
  found`), contradicting this section's own earlier (unverified) claim that
  a same-block `*CONN`-declared bare name would resolve. Only a
  colon-scoped, two-part identifier (`*I <inst>:<pin>` or `<net>:<N>`)
  resolves as a two-node endpoint, without exception.
- SPEF's `*<N>` positional shorthand (referencing the Nth `*CONN` entry by
  index instead of by name) and a `PIN:<port>`-style pseudo-instance form
  were both tried as alternate ways to reference a top-level port pin from
  inside a `*RES`/`*CAP` entry; neither changed the outcome described below.

**Live re-measurement of the full fix, `gcd`** (fresh `klt synthesize` →
`klt place-and-route --post_route_spef` run, not the committed corpus
fixture): a raw OpenSTA session's `report_parasitic_annotation`, run
immediately after `read_spef` against the freshly written SPEF, now reports

```
Found 3 unannotated drivers.
Found 52 partially unannotated drivers.
```

— down from **533** (the pre-topology-fix baseline measured above) to
**52**, a better than 90% reduction. `spef_sta.worst_slack_ns` /
`.total_negative_slack_ns` on the same run came back `-2.09926` /
`-79.5244`, genuinely **different from, and more pessimistic than**, the
top-level (rung 2) `worst_slack_ns` / `total_negative_slack_ns` of
`-1.94402` / `-72.6797` — the first time `read_spef` has ever changed this
design's measured slack in either direction. This is the acceptance
criteria's "`worst_slack` changes (pessimistically) across `read_spef`"
requirement, now genuinely met and live-verified. `klt`'s own
design-side annotation check (`spef_sta.design_nets_annotated` /
`.design_nets_total`) stays `537 / 537`, unaffected — it measures net-name
correlation, not RC-topology completeness, and was already saturated before
this fix.

**The residual 52 drivers were narrowly characterized in the prior pass,
and the root mechanism was found and fixed (issue #961's own residual,
2026-08-14).** Every one of the 52 involved a connection directly adjacent
to a **top-level design port pin** — either an input port (`a_in[0]`,
`clk`, `rst_n`, …) whose fanout to a real `*I`-declared cell pin was
flagged unannotated, or an internal driver pin (`_747_/Q`, …) whose
connection to a top-level *output* port (`result[11]`) was flagged.

**Root cause, found by cross-checking against OpenSTA's own shipped SPEF
test fixture (`examples/gcd_sky130hd.spef` in
`The-OpenROAD-Project/OpenSTA`) and by reading `SpefReader::findParasiticNode`
directly.** OpenSTA's SPEF reader resolves a *bare* (colon-free) `*RES`/
`*CAP` node through `findPortPinRelative` — a `Network::findPin` lookup on
the design's own top-level pin — **not** through any net-name lookup. A
bare port name genuinely *is* a valid two-node endpoint there (the prior
pass's own live reproductions, which concluded otherwise, did not isolate a
bus-indexed port from a non-bus one — see below for why that distinction
turned out to matter): `examples/gcd_sky130hd.spef`'s own `*RES 1 clk
*198:13 46.6763` line ties its `clk` port directly to an internal node with
zero parse warnings and zero unannotated loads, confirmed by running that
exact fixture's own OpenSTA regression test
(`parasitics/test/parasitics_gcd_spef.tcl`) against a plain `sta` binary.
Reproducing the identical shape for this repo's own routed `gcd`'s
non-bus ports (`clk`, `done`) confirmed the same result live.

**The remaining piece was bus-index bracket escaping.** A **bus-indexed**
port (`a_in[0]`, `result[13]`) only resolves the *same* way when its
brackets are left **un-escaped** (`a_in[0]`, not `a_in\[0\]`) — this
writer's own general escaping helper (`_spef_name`, used everywhere else in
the file) backslash-escapes `[`/`]`, which tells the reader "this is a
literal backslash-bracket character, not a bus index" per this file's own
`*BUS_DELIMITER [ ]` declaration, so `findPin` looks for a pin literally
named `a_in\[0\]` — which does not exist, only the real, bus-expanded
`a_in[0]` pin does — and warns `pin a_in\[0\] not found`. Isolated live by
testing `a_in[0]` (escaped, fails), `clk`/`done` (no brackets, already
worked), and `a_in[0]` un-escaped (works) as three separate single-net SPEF
reproductions against this repo's own routed `gcd` design.

**The fix (`_spef_port_node_name`, `src/klayout_tools/extract.py`):** for
any net that is both a declared port *and* unambiguously named
(`name_counts[net] == 1` — the same duplicate-name guard `--def-net-connections`
already uses, so a shared label like `VGND`'s un-strapped islands keeps the
pre-fix internal-node behavior), that net's own `net_node`/`hub_node` (see
above) becomes the port's own bare name, brackets left un-escaped, instead
of the internal `<net>:1` node. Every other identifier position — the
`*D_NET`/`*PORTS`/`*P` text, `*I <inst>:<pin>` device-pin references, and
every non-port net's internal `<net>:<N>` nodes — is unaffected and keeps
escaping brackets as before; this is a narrowly-scoped exception to one
specific identifier position for unambiguously-named port nets only.

**Live-verified end to end (2026-08-14, `openroad/orfs:latest`, fresh `klt
synthesize` → `klt place-and-route --post_route_spef` run, not the
committed corpus fixture, real `read_db`/`read_liberty`/`create_clock`/
`read_spef` session):**

```
Found 3 unannotated drivers.
Found 0 partially unannotated drivers.
```

**Zero partially unannotated drivers** — down from 52 (this section's own
prior residual) and 533 (the pre-topology-fix baseline) — satisfying the
issue's own "no partially unannotated drivers" acceptance criterion in
full. The 3 fully-unannotated drivers (`clkload0/Y`, `clkload1/Y`,
`clkload2/Y`) are unused clock-load dummy buffers with genuinely no
fanout — a correct, pre-existing report, not a defect. `spef_sta.worst_slack_ns`
stays `-2.09926` (unchanged by this fix, as expected: it was already
correctly wired for the paths it changed; the residual only affected
`report_parasitic_annotation`'s own completeness bookkeeping for
port-adjacent connections, never the delay values already attached through
the rest of the RC network). The pre-existing, unrelated `net VPWR/VGND not
found` warning count (1001, `gcd`'s 105 un-strapped power-net islands, "a
known, inherited limitation" per `docs/cli/extract.md`'s "Duplicate net
names" section) is unchanged by this fix.

Off by default: this adds real wall-clock cost on top of every existing
`"route"`-stage caller (one more `klt extract --parasitics` pass over the
merged GDS, plus one more `openroad` subprocess invocation), matching this
command's own convention for every other flag whose added cost a survey
flagged rather than assumed free (`route_critical_nets_percentage`,
`max_antenna_repair_iterations` above). Has no effect for a request whose
`target_stage` does not reach `"route"` — `spef_sta` stays `null`, mirroring
`def_path`/`gds_path`'s own pre-`"route"` `null`.

**Not (yet) in this contract**: a combined "three-point fidelity ladder"
comparing this field against `klt-statime-native`'s own pre-route wire-free
`sta` value (`klt synthesize`'s response, a separate command entirely) — a
caller building that comparison runs both commands and diffs the two JSON
responses directly; nothing here fabricates a cross-command field.

**Relationship to the multi-corner sweep above.** Survey §4.2's corner sweep
has since shipped (issue #949) and is *orthogonal* to this field rather than
layered on it: it re-estimates parasitics per corner (`estimate_parasitics
-global_routing`) across every `.lib` corner the resolved `cell_library`
ships, while `spef_sta` reads real extracted parasitics at the single
resolved `pdk.corner`. A `"route"`-stage run reports both axes independently
— the sweep always, `spef_sta` only under `post_route_spef: true` — in
separate OpenROAD invocations that share neither script nor `-metrics` file.
OCV derating (survey §4.4) remains a separate follow-on this field's own SPEF
artifact is meant to feed; SDF write-back (survey §4.3) has since shipped as
`post_route_sdf`, immediately below.

### SDF export (`post_route_sdf`, issue #1002)

Survey §4.3 names `write_sdf` the upstream half of an SDF-annotated
gate-level re-simulation, and is explicit about where it belongs: *"after
§4.1's `read_spef`, so the written SDF reflects real routed-parasitic delays,
not the global-routing estimate."* The optional boolean `post_route_sdf`
request field (default `false`) adds exactly that — one `write_sdf` call
inside the **same** `post_route_spef` OpenSTA session, immediately after its
`read_spef` and before any report command:

```tcl
read_spef  .../gcd_route.spef
write_sdf -divider . -include_typ .../gcd_route.sdf
```

The written file is reported as **`spef_sta.sdf_path`** (`null` when not
requested, mirroring `spef_path`'s own shape). Its delays are the ones that
session computed from the design's own resolved liberty (`request.pdk.corner`)
plus the extracted routed parasitics — never a synthetic or uniform delay
model.

- **`post_route_sdf: true` requires `post_route_spef: true`**, and says so
  (exit 1) rather than quietly writing nothing. That is the whole point of
  §4.3's sequencing: an SDF written from a session fed by `estimate_parasitics
  -global_routing` would carry the coarse estimate's delays while *looking*
  exactly like a post-route measurement to every downstream simulation that
  annotates it.
- **`-divider .`** — SDF hierarchy divider. OpenSTA's own default is `/`, but
  the consumer is a Verilog simulator whose hierarchy separator is `.`;
  emitting `/` would leave every `INSTANCE` name unmatchable.
- **`-include_typ`** — populate the *typ* member of every `min:typ:max`
  triplet. Icarus selects a triplet member at compile time (`iverilog -T
  min|typ|max`, defaulting to `typ`), so a triplet with an empty typ member —
  OpenSTA's default — would leave the default corner selecting nothing.
- **A reported-but-missing file is an error**, not a silent success: a
  `sdf_path` a downstream run cannot open surfaces there as a *silent*
  zero-delay simulation, since Icarus treats an unopenable SDF as a non-fatal
  `SDF WARNING` and `vvp` still exits `0`
  ([`docs/design/sdf-annotate-feasibility-spike.md`](../design/sdf-annotate-feasibility-spike.md)
  §3.3).

The artifact's consumer is [`klt functional-verification`](functional-verification.md)'s
own `options.sdf` block — hand it `spef_sta.sdf_path` and the design's
existing gate-level testbench re-runs with real post-route delays applied.

**Verification status**: the generated Tcl is asserted by unit tests
(`tests/test_place_and_route.py`), **not** yet re-measured against a real
`openroad`/OpenSTA session — no `openroad` binary or `openroad/orfs`
container was available when this shipped, the same constraint recorded for
issues #961/#996 above. The Icarus half *was* verified live against real
`iverilog` 13.0 (see `functional-verification.md`'s "SDF back-annotation").

### Measured fidelity ladder (`gcd` / `modexp` / `mult8`, sky130A, `seed: 1`)

Run live against `openroad/orfs:latest` (OpenROAD `26Q3-1080-gab6fd26351`),
sky130A, `utilization_pct: 38`, `target_stage: "route"`. Each rung is one
JSON field from one of the two commands, diffed by hand rather than by a
field this contract fabricates:

Re-measured end to end on 2026-08-14 (issue #951). Every rung below comes
from one live run of the current tree — none are carried over from the
#948-era table, since intervening synthesis changes moved `modexp`'s and
`mult8`'s netlists (`gcd`'s rungs 1 and 2 reproduce bit-identically).

| Rung | Source | `gcd` (1.1 ns) | `modexp` (5.0 ns) | `mult8` (6.0 ns) |
|---|---|---|---|---|
| 1 — pre-route, wire-free | `klt synthesize` → `sta.worst_path.delay_ns` (`klt-statime-native`) | 2.86419 ns | 4.63728 ns | 3.74825 ns |
| 2 — post-route, global-routing RC estimate | `klt place-and-route` → top-level `worst_slack_ns` / `total_negative_slack_ns` | −1.94402 / −72.6797 ns (50 setup viol.) | −0.03895 / −0.20145 ns (10 setup viol.) | 1e+39 / 0 (unconstrained) |
| 3 — post-route, `read_spef` of real extracted RC | `klt place-and-route` → `spef_sta.worst_slack_ns` / `.total_negative_slack_ns` | −2.09926 / −79.5244 ns (50 setup viol.) | −0.20604 / −2.20498 ns (16 setup viol.) | 1e+39 / 0 (unconstrained) |
| — net-name correlation, design-side | `spef_sta.design_nets_annotated` / `.design_nets_total` | **537 / 537** | **760 / 764** | **276 / 276** |
| — net-name correlation, SPEF-side | `spef_sta.nets_annotated` / `.nets_total` | 537 / 1356 | 760 / 2458 | 276 / 655 |

`read_spef` accepted the written SPEF **without error on all three
designs**, and — since issue #951 — resolves essentially every net name in
it against the linked design.

**Both `gcd`'s and `modexp`'s rung 3 above reflect issue #961's reworked RC
topology (PR #984's `<net>:<N>` two-node endpoints plus this issue's own
residual fix, a unique port's own bare-name node, 2026-08-14) and now read
pessimistically relative to rung 2, as the acceptance criteria require** —
`gcd`'s `spef_sta.worst_slack_ns` / `.total_negative_slack_ns` moved from
`-1.72532` / `-63.8855` (the net-name-only, pre-topology-fix baseline this
table carried since issue #951) to `-2.09926` / `-79.5244`, genuinely more
negative than rung 2's `-1.94402` / `-72.6797`; `modexp`'s moved from
`+0.26564` / `0` (the same pre-topology-fix baseline — genuinely
*optimistic* relative to rung 2's `-0.03895` / `-0.20145`, the exact defect
this issue tracks) to `-0.20604` / `-2.20498`, now likewise more negative
than rung 2. Both designs' `report_parasitic_annotation` (run live in the
same sessions) reports **0** partially unannotated drivers — `gcd`'s down
from 52 after PR #984's topology rework alone (and 533 before any of this
issue's fixes); `modexp`'s 23 *fully* unannotated drivers are the design's
own 4 net-name-unmatched nets' fanout (`spef_sta.design_nets_annotated` /
`.design_nets_total` = 760 / 764 above — a correct, pre-existing report,
not a defect this issue tracks) — see "`*CONN` device-terminal pin
correlation" above for the full live-verification log and the exact fix.
`mult8` is unaffected regardless (purely combinational, both post-route
rungs report OpenSTA's own unconstrained `1e+39` sentinel either way, so
there is no rung-3-vs-rung-2 comparison to make there).

Historical note, retained for context: as measured on 2026-08-14 (issue
#951, pre-`*CONN`-correlation at all — before either #961 increment), rung 3
read optimistically relative to rung 2 because `read_spef` matched every net
by name but discarded its RC network outright (the `*CAP`/`*RES` node names
correlated to no pin in the design at all, since device-terminal
connectivity was not emitted). The DEF-driven `*CONN` correlation that
landed first (`def_net_connections`/`--def-net-connections`, still in
place) supplies the `*I <inst>:<pin>` `*CONN` entries themselves, but on its
own still used a bare net name as the `*RES` leg's other endpoint — the
defect this section's topology rework (above) actually fixes.

`klt drc`/`klt lvs` are unchanged by this section's SPEF-writer topology
rework — it only changes the *text* `klt extract --spef` writes for
`*RES`/`*CAP` node identifiers, touching no geometry or connectivity either
command reads, and `route_drc_violation_count` stayed `0` on `gcd` in the
live re-measurement above.

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
  "power": {
    "power_net": "VDD",
    "ground_net": "VSS",
    "straps": [
      { "layer": "met1", "width_um": 0.48, "pitch_um": 5.44, "followpins": true },
      { "layer": "met4", "width_um": 1.6, "pitch_um": 27.14, "offset_um": 13.57 },
      { "layer": "met5", "width_um": 1.6, "pitch_um": 27.2, "offset_um": 13.6 }
    ]
  },
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
| `power` | object \| omitted | Power delivery (tapcell/PDN/fillers) — see "Power delivery" below. Omitted (the default) preserves prior behavior exactly: no `SPECIALNETS`, no tapcells/fillers. |
| `power.power_net` / `.ground_net` | string \| omitted | Power/ground net names. Default `"VDD"`/`"VSS"`. Must differ from each other. |
| `power.straps` | array\<object\> | Required when `power` is given, non-empty. Each entry: `layer` (string, required), `width_um`/`pitch_um` (positive numbers, required), `offset_um` (number, default `0`), `followpins` (boolean, default `false`). Listed bottom-to-top; each consecutive pair is connected by an `add_pdn_connect` call. |
| `constraints.clock_port` / `.clock_period_ns` | string / number | Clock port name + target period (ns). Required once `target_stage` reaches `"place"` or later — stages beyond floorplan have no meaning without a clock. |
| `seed` | integer | Placement/routing seed. **Required** — P&R is genuinely stochastic; a stored result must be reproducible. Echoed unchanged in the response. |
| `target_stage` | string | One of `"floorplan"`, `"place"`, `"cts"`, `"route"` (default) — how far this run is asked to go. See "Partial completion" below. |
| `route_critical_nets_percentage` | integer \| omitted | 0–100, default `0` (no flag emitted). Percentage of worst-slack nets `global_route` treats as timing-critical during congestion-removal iterations (`-critical_nets_percentage`, issue #939). `0` reproduces this command's prior behaviour exactly — the A/B disable path. Not evaluated with a real OpenROAD A/B run as of this field's introduction; see `place_and_route.py`'s module docstring for the audit methodology and its limitations. |
| `max_antenna_repair_iterations` | integer \| omitted | 1–8, default `1` (today's exact single-pass behaviour). Repeats the `"route"` stage's `repair_antennas`/`detailed_route` reroute pair this many times (issue #939), a bounded flow-level generalisation of the single pass issue #759 shipped. No early exit on a zero-violation `check_antennas` result — every pass runs unconditionally. |
| `post_route_spef` | boolean \| omitted | Default `false`. Opts in to the real-parasitics A/B pass described in "Post-route SPEF STA" above — populates the response's `spef_sta` field. Off by default (real added wall-clock cost); has no effect unless `target_stage` reaches `"route"` (issue #948). |
| `post_route_sdf` | boolean \| omitted | Default `false`. **Requires `post_route_spef: true`** (exit 1 otherwise). Adds one `write_sdf` call to that same post-`read_spef` OpenSTA session and reports the written file as `spef_sta.sdf_path` — see "SDF export" above (issue #1002). |

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
  "route_drc_violation_count": 0,
  "worst_setup_slack_ns": -4.02163,
  "worst_hold_slack_ns": 0.08421,
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
  "layer_map": {
    "path": "/abs/path/pdk/sky130A/libs.tech/klayout/tech/sky130A.map",
    "resolution": "exact"
  },
  "verilog_path": "/abs/path/.klt/place-and-route/gcd.v",
  "spef_sta": {
    "spef_path": "/abs/path/.klt/place-and-route/gcd_route.spef",
    "sdf_path": "/abs/path/.klt/place-and-route/gcd_route.sdf",
    "worst_slack_ns": -1.72532,
    "total_negative_slack_ns": -63.8855,
    "setup_violation_count": 50,
    "hold_violation_count": 0,
    "nets_annotated": 537,
    "nets_total": 1356,
    "design_nets_annotated": 537,
    "design_nets_total": 537,
    "annotation_complete": true,
    "annotation_warning": null
  },
  "power": {
    "pdn": true,
    "global_connect": true,
    "power_net": "VDD",
    "ground_net": "VSS",
    "tapcell_master": "sky130_fd_sc_hd__tapvpwrvgnd_1",
    "endcap_master": null,
    "filler_masters": [
      "sky130_fd_sc_hd__fill_1",
      "sky130_fd_sc_hd__fill_2",
      "sky130_fd_sc_hd__fill_4",
      "sky130_fd_sc_hd__fill_8"
    ]
  },
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
| `route_drc_violation_count` | integer \| null | The violation count from `detailed_route -output_drc <rpt>`'s own report (TritonRoute's routing-legality check — short/spacing/via/etc. violations, distinct from the antenna check above), parsed from the report's per-violation `"violation type: ..."` header lines. `0` for a DRC-clean route (a real `-output_drc` report is a 0-byte file in that case, not absent). `null` before the `"route"` stage — no `detailed_route` call has run yet (issue #938). |
| `worst_setup_slack_ns` / `worst_hold_slack_ns` | number \| null | The corner-swept worst-case setup/hold slack — see "Multi-corner setup/hold sweep" below. `null` before the `"route"` stage; distinct from (and does not replace) `worst_slack_ns`, which stays the single nominal-corner value it has always been (issue #949). |
| `estimated_power_mw` | number \| null | `null` before placement. |
| `clock_skew_ns` | number \| null | Worst setup-side clock skew (`report_clock_skew_metric -setup`) across the clock tree TritonCTS built. `null` before the `"cts"` stage — no clock tree exists yet, so there is nothing to measure skew across (issue #783). |
| `stages` | array\<object\> | One entry per completed stage through `stage_reached`, each with whatever subset of the top-level metric fields that stage's own OpenROAD reports populate. The top-level fields above are always the **last** entry in `stages`, restated at top level. |
| `macros` | array\<object\> | Echo of the request's `macros[]` (`instance`/`lef`/`x_um`/`y_um`/`orientation`; `lef` resolved to an absolute path). `[]` when the request declared none. |
| `def_path` | string \| null | Populated once `write_def` has run (i.e. `stage_reached` is `"route"`); `null` otherwise. |
| `gds_path` | string \| null | Populated only once the DEF→GDS merge has also completed; `null` otherwise. |
| `layer_map` | object \| null | Additive field (issue #1029). `null` unless `stage_reached` is `"route"`, mirroring `gds_path`. `path` — the absolute path to the open_pdks KLayout LEF/DEF layer-map file actually applied to the DEF→GDS merge, or `null` if none was found. `resolution` — `"exact"` when a variant-named file (`<variant>.map`, e.g. `sky130A.map`) matched; `"family"` when no variant-named file existed and the family-level fallback (`<family>.map`, e.g. `gf180mcu.map` for `gf180mcuC`/`gf180mcuD`, whose open_pdks install ships only that shared file — see `_resolve_layer_map`) matched instead; `"none"` when neither existed, in which case the merge proceeded without a guaranteed-matching layer/datatype assignment for routing shapes, matching `def2stream.py`'s own degrade-gracefully behavior. |
| `verilog_path` | string \| null | Additive field (issue #996). The **as-built** gate-level Verilog netlist — OpenROAD's own `write_verilog` output, written from the same linked design `write_def` dumped, so it describes the exact design state `def_path`/`gds_path` implement (CTS buffers, `repair_design`/`repair_timing` resizes, and `repair_antennas` diodes all included). Populated once the `"route"` stage has run (i.e. `stage_reached` is `"route"`); `null` otherwise, exactly like `def_path`. See "As-built netlist (`verilog_path`)" below. |
| `spef_sta` | object \| null | Additive field (issue #948; `design_nets_*` added by #951). `null` unless `post_route_spef: true` **and** `stage_reached` is `"route"`. `spef_path` — the written SPEF file. `sdf_path` (issue #1002) — the written IEEE-1497 SDF file, or `null` unless `post_route_sdf: true`; see "SDF export". `worst_slack_ns`/`total_negative_slack_ns`/`setup_violation_count`/`hold_violation_count` — the `read_spef`-fed re-report, directly comparable to the top-level fields above (same design, same checkpoint, different parasitics source). `nets_annotated`/`nets_total` — SPEF-side correlation (`get_nets -quiet` against every SPEF-declared net name, run before `read_spef`); flat extraction also emits intra-standard-cell nodes the gate-level design never had, so this ratio cannot reach 1 by construction. `design_nets_annotated`/`design_nets_total` — design-side correlation: how many of the nets OpenSTA times the SPEF names at all; **check this pair before trusting the timing numbers**. `annotation_complete` — `true` only when the design-side pair is equal and non-zero. `annotation_warning` — `null` when complete, otherwise a sentence naming the shortfall and stating that the timing values are not a real-parasitics measurement to the extent annotation is missing. |
| `power` | object | Additive field (issue #1091). Always present (never `null`) so a caller can tell a signal-only "route" result from a power-complete one without parsing the DEF for a missing `SPECIALNETS` section — see "Power delivery" below. `pdn`/`global_connect` — `false`/`false` unless `request.power` was given, in which case both are `true` (they always run together, at the end of the `"floorplan"` stage). `power_net`/`ground_net` — echo of the request (or its `"VDD"`/`"VSS"` defaults), `null` when `request.power` was omitted. `tapcell_master`/`endcap_master` — the per-library masters `tapcell` actually used, `null`/`null` when `request.power` was omitted. `filler_masters` — the per-library masters the `"route"` stage's own `filler_placement` call used; `[]` unless `request.power` was given **and** `stage_reached` is `"route"` (`filler_placement` is a `"route"`-stage-only call). **Not** a live placed-instance count — see "Power delivery" below. |
| `provenance` | object | The shared envelope block (`docs/json-contract.md`). `deck` names the resolved liberty file (`<cell_library>__<corner>`); `pdk` is `find_pdk()`'s resolved triple; `input` is the content hash of `netlist`. |

## As-built netlist (`verilog_path`, issue #996)

The `"route"` stage writes an OpenROAD `write_verilog` netlist to
`<output_dir>/<hdl_toplevel>.v`, immediately after its `write_def`, from the
same linked design. `verilog_path` names it.

**Why it exists.** This command inserts and modifies cells: TritonCTS builds
a clock tree (`clock_tree_synthesis`), `repair_design`/`repair_timing` insert
buffers and swap gates for higher-drive-strength variants, and
`repair_antennas` inserts diodes. None of that is visible in `klt
synthesize`'s netlist, which is produced *before* place-and-route runs. So
using the synthesis netlist as the reference for a gate-level LVS against the
routed GDS produces mismatches that are not defects and that `klt lvs` cannot
attribute — in one real run, 40 of ~720 instances (35 CTS/timing-repair cells
plus 5 drive-strength resizes under unchanged instance names). `verilog_path`
is the netlist that actually corresponds to `def_path`/`gds_path`, and is
what a golden-reference LVS run should build its reference from.

Typical use:

```bash
klt place-and-route pnr_request.json --format json   # -> verilog_path, gds_path
klt extract "$gds_path" --deck sky130 --abstract-cells 'sky130_fd_sc_hd__*' \
  -o layout.spice                                    # layout side
# reference side: build from $verilog_path, not from klt synthesize's netlist
klt lvs lvs_request.json
```

> **Note.** `klt lvs` compares SPICE netlists, and `klt extract
> --abstract-cells` emits a hierarchical SPICE subcircuit netlist for the
> layout side. Converting `verilog_path`'s gate-level Verilog into the
> matching SPICE reference is a separate, still-deferred capability (see
> `docs/cli/extract.md`'s "Gate-level Verilog output" limitation). This field
> removes the *structural* blocker — an as-built netlist now exists at all —
> it does not by itself complete the Verilog→SPICE reference path.

**Flags.** The command is written plain: `write_verilog <path>`.
`-include_pwr_gnd` is deliberately not passed, matching ORFS's own
`6_final.v` and keeping the artifact directly diffable against `klt
synthesize`'s netlist (which likewise carries no VPWR/VGND connections —
power comes from the LEF/DEF grid, not the netlist). `-remove_cells
[find_physical_only_masters]` is not passed either: this flow inserts no
fill/tap/endcap cells, so it would be a no-op that only risks dropping a real
cell. Antenna diodes are real instances present in the routed GDS and are
kept. `-sort` is ignored by OpenROAD itself (`utl::warn STA 2065`).

**`"route"` only, never `"cts"`.** The artifact exists to be the netlist
counterpart of a routed layout, and `"route"` is the only stage that produces
one. A `"cts"`-stage netlist would describe a state no shippable layout
corresponds to (it predates `repair_antennas`'s diode insertions) and would
need a second response field with no `def_path`/`gds_path` to pair with.
`verilog_path` therefore follows those two fields exactly.

## Power delivery (`request.power`, issue #1091)

Before this field existed, this command's generated Tcl never called
`global_connect`/`pdngen` and never inserted tapcells or filler cells: the
routed DEF it wrote had no `SPECIALNETS` section at all, every standard
cell's `VDD`/`VSS` LEF pin belonged to no net, and cell rows were
discontinuous wherever placement left a gap. `target_stage: "route"`
returning `status: "ok"` therefore meant "signal routing completed," not
"this design has been placed and routed" in the sense a caller taking
`def_path`/`gds_path` onward would assume — DRC (well/substrate ties, rail
continuity), LVS (power nets in the reference netlist), and any real handoff
all need power delivery first.

The optional `request.power` block closes this gap. Net names for the
power/ground rails (`power_net`/`ground_net`, default `"VDD"`/`"VSS"`) plus
the PDN strap geometry (`straps[]` — `layer`/`width_um`/`pitch_um`, plus
optional `offset_um`/`followpins`, listed bottom-to-top) drive, at the end
of the `"floorplan"` stage (immediately after `place_macro`/`make_tracks`,
before that stage's own `write_db`):

1. `tapcell` — well/substrate ties, using the per-library master + distance
   this command already knows (sourced the same verified-not-guessed way as
   the CTS buffer/routing-layer-range/antenna-diode tables — see
   `place_and_route.py`'s own module docstring).
2. `add_global_connection` (one call per per-library pin-pattern rule) +
   `global_connect` — wiring every standard cell's PG pin to the named
   power/ground net.
3. `set_voltage_domain` + `define_pdn_grid` + `add_pdn_stripe` (one per
   requested strap) + `add_pdn_connect` (between each consecutive strap
   pair) + `pdngen` — the strap/rail grid itself.

...and, at the end of the `"route"` stage (immediately after the
antenna-repair loop, before `write_def`):

4. `filler_placement` — closing every row gap, using the per-library filler
   masters this command already knows.
5. A second `global_connect` — wiring the newly-placed filler instances'
   own PG pins, mirroring OpenROAD-flow-scripts' own
   `flow/scripts/final_connect.tcl`, whose own comment states exactly why:
   "Ensure all OR created (rsz/cts) instances are connected."

This insertion ordering mirrors OpenROAD-flow-scripts' own stage sequence
exactly (`flow/scripts/tapcell.tcl` → `pdn.tcl` → global placement;
`detail_route.tcl` → `fillcell.tcl` → `final_connect.tcl`).

`request.power` omitted (the default) preserves prior behavior exactly — no
`SPECIALNETS`, no tapcells/fillers, and the response's `power` field reports
that nothing ran.

**Response `power` field: what it reports, and what it doesn't.** `pdn`/
`global_connect`/`power_net`/`ground_net`/`tapcell_master`/`endcap_master`
name what this run was *configured* with — not a live placed-instance
count. OpenROAD reports real per-master instance counts only via a
`report_design_area`/`get_cells -filter`-style query this command does not
yet thread through its per-stage `-metrics <file>.json` mechanism (the same
mechanism `stages[]`'s own metric fields already use); adding that is a
natural, separable follow-up. `filler_masters` is `[]` unless `stage_reached`
is `"route"` (`filler_placement` is a `"route"`-stage-only call) — it names
the masters passed to that call, not how many filler instances OpenROAD
actually placed.

**Macro-specific PDN grids are out of scope for this v1.** `pdngen` here
builds only the flat standard-cell grid (`define_pdn_grid` with no
`-macro`) — a design with hard macros needs a caller-supplied macro
halo/grid spec this field does not yet expose.

**`write_verilog` strips the new physical-only cells.** When `request.power`
is set, the `"route"` stage's `write_verilog` call (see "As-built netlist"
above) additionally passes `-remove_cells` naming the tapcell/endcap/filler
masters this run used — keeping `verilog_path` diffable against `klt
synthesize`'s own netlist, which never contains them.

## Partial completion (`target_stage`)

A request with `target_stage: "place"` asks only for floorplan through
detailed placement — a successful (`exit 0`) run of that request has
`stage_reached: "place"`, `def_path`/`gds_path`/`verilog_path` all `null`
**by design** (never requested), and every metric field populated through
placement. This is a normal, successful, partial-by-request outcome, not a
degraded one.

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

### Diagnosed engine errors: `DRT-0305` (constant-tie nets)

One OpenROAD error is translated rather than echoed. If the netlist handed
to this command still contains bare `1'b0`/`1'b1` constant drivers, OpenSTA's
Verilog reader materialises one net per constant value (conventionally
`zero_`/`one_`), OpenROAD types those nets `GROUND`/`POWER`, and TritonRoute
aborts the `route` stage with `DRT-0305`. OpenROAD prints the informative
line on stdout and an uninformative Tcl summary (`Error:
pnr_<top>_route.tcl, 6 DRT-0305`) on stderr, so the default surface was the
script line number and nothing else. The `error.message` now names the
offending net and the fix instead (issue #854):

```
openroad 'route' stage failed: [ERROR DRT-0305] Net zero_ of signal type GROUND is not routable by TritonRoute. -- net 'zero_' is a constant tie, not a real signal: … Re-synthesize the netlist so its constants are driven by real tie cells …
```

Netlists produced by `klt synthesize` no longer hit this: it maps constants
onto the resolved library's own tie cells via Yosys's `hilomap` pass — see
[`docs/cli/synthesize.md`](synthesize.md)'s "Constant ties" section. The
diagnosis remains for netlists produced by anything else.

## Out of scope

- **Metal fill (density fill), `DONT_USE_CELLS`-style cell exclusion.** A
  core-only block v1, matching the contract's own IO-ring/footprint
  exclusion — neither is part of the request/response contract this phase
  implements, and each can be added later as an additive request field
  without a contract-shape change. (Hard-macro placement and tapcell/PDN
  power delivery were originally scoped out here too — see "Hard-macro
  placement" above and "Power delivery" below; issues #438 and #1091 closed
  those gaps.)
- **Macro-specific PDN grids.** `request.power`'s `pdngen` call builds only
  the flat standard-cell grid (`define_pdn_grid` with no `-macro`) — a
  design with hard macros needs a caller-supplied macro halo/grid spec this
  field does not yet expose. See "Power delivery" below.
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
