# Changelog

`klt` has not yet reached `1.0`; per [`docs/json-contract.md`](docs/json-contract.md),
`schema_version` only bumps for non-additive (breaking) shape changes to a
command's own payload. Additive behavior changes — including new
`mismatches[].category` values `klt lvs` can emit — land under the same
`0.1.0` package version and are recorded here instead. This file is the
source of truth for which categories exist as of a given date; pin
`provenance.deck` (sha256) and `provenance.klayout_version`, not
`klt --version`, if you need to detect this kind of drift.

## 0.1.0 (2026-07-31)

Initial release — the agent-native IC layout toolkit, first cut.

### Added

- `klt` CLI with five headless, JSON-contracted verbs:
  - `klt layers` — layer/datatype enumeration for GDSII/OASIS streams
  - `klt stats` — bounding box, drawn area, density, polygon/vertex counts (`--per-layer`)
  - `klt cells` — cell hierarchy: top cells, shape/instance counts, bboxes (`--top`)
  - `klt drc` — headless DRC via KLayout's native Region check primitives, with
    curated width/space/enclosure decks for sky130 and gf180mcu (`--deck`)
  - `klt pdk find|list|env` — discovery/resolution of open_pdks-layout PDK installs
- Shared JSON output envelope (`schema_version`, error shape, exit codes) across
  all verbs — `docs/json-contract.md` is the API
- `scripts/fetch-pdks.sh` — pinned fetch of lambdapdk open PDK data
- `kb/` knowledge-base scaffold with JSON Schema and seed entries
- sky130/gf180mcu test corpus with golden fixtures; CI (ruff + pytest, Python 3.10–3.13)
- Docs: architecture, JSON contract, per-verb CLI references, macOS KLayout
  source-build guide; site at klayout-tools.org

### Added since release (still reporting `0.1.0`)

`klt` has grown considerably since the initial release above without a
version bump (see the pre-1.0 note at the top of this file); the entries
below are the user-visible, additive behavior changes worth calling out
explicitly because they affect a verb's output under an unchanged reported
version. Not an exhaustive commit-by-commit log.

- Since 0.1.0 the CLI has grown from 5 verbs to 22: `layout-metrics`,
  `render`, `extract`, `lvs`, `gen`, `gen-compose`, `draw`, `sim`, `kb`,
  `precheck`, `socket-check`, `ring-check`, `report`, `trajectory`,
  `synthesize`, `place-and-route`, and `functional-verification` were added
  on `main`. Each is documented in [`docs/cli/`](docs/cli/); the next
  release will carry them collectively.
- 2026-08-04 — `klt lvs`: new `device.combine_incomplete` mismatch category
  (#466). Only possible with `options.combine_devices: true`. KLayout's own
  `klayout.db.Netlist.combine_devices()` can raise an unhandled
  internal-consistency `RuntimeError` on a *partial-match* device group — N
  real (matching-relevant) instances plus M dummy instances that all share
  two of three terminals (e.g. a bipolar device's base and collector, tied
  to a matched array's common well and substrate), but only the N real
  instances additionally share the third (e.g. an emitter bussed to one
  signal net). `klt lvs` now catches that one error shape per netlist
  (narrowly — only a `RuntimeError` carrying KLayout's own `"...in
  Netlist.combine_devices"` marker text; any other `RuntimeError` still
  propagates as an application error) instead of letting it abort the whole
  run: whatever `combine_devices()` already merged stays merged, that
  netlist's remaining devices are left as individual devices, and a
  `severity: "warning"` entry (never changes `status`, never breaks
  `mismatch_count`'s error semantics) records that combine did not fully
  apply on that side. Purely additive (no `schema_version` bump), but adds a
  `category_counts["device.combine_incomplete"]` entry for any
  `combine_devices` run that trips the KLayout error — see
  `docs/cli/lvs.md`'s `device.combine_incomplete` subsection for the full
  trigger conditions.
- 2026-08-04 — `klt gen` + `klt gen-compose`: ring routing openings (#434).
  `guard_ring`, `diff_pair` (`add_guard_ring`) and `bjt_array`
  (`add_collector_ring`) accept `ring_gap_side` (`""`/`"N"`/`"S"`/`"E"`/
  `"W"`), `ring_gap_um` and `ring_gap_offset_um`, cutting **one** opening
  through the ring's band — on every layer the ring is drawn on, dropping
  any contact it would clip — so the ring stays a single connected C-shaped
  conductor rather than splitting into two arcs. The opening is reported as
  a new `GAP_<side>` entry in `ports[]` (`width_um` = the opening's length
  along that side, `direction_deg` = the side's outward normal), and
  `klt gen-compose` uses it to admit a route to a ringed block's non-tap
  port — but only when the drawn backbone actually passes through the
  opening with half the route width plus the block's own
  `drc_hints.min_spacing_um` of clearance from either cut end; a crossing on
  any other side, a crossing that misses the opening, and a segment laid
  along a ring side are all still reported in `unrouted_nets[]`. With no
  opening declared, the #199 closed-ring rejection is unchanged (its message
  now also names the new remedy). Wiring or labelling a `GAP_*` port is an
  application error (exit `1`). Before this, a matched analog group could
  keep its default guard/collector ring **or** be wired into the rest of a
  composed circuit, not both. See `docs/cli/gen.md` ("Ring routing
  openings") and `docs/cli/gen-compose.md`.
- 2026-08-03 — **Breaking (per-command `schema_version` bump 1 -> 2):** `klt
  precheck`: `layer_whitelist` violations' `shapes` count is now weighted by
  placement multiplicity across the full cell hierarchy, not summed once per
  cell *definition* (issue #452). Previously, `layout.each_cell()` counted
  each cell definition's own (non-recursive) shapes exactly once regardless
  of how many times that cell was actually placed, under-reporting true
  placed-shape prevalence by roughly two orders of magnitude on real
  hierarchical, macro-scale input — e.g. a layer drawn 800 times across 320
  placed `sky130_fd_sc_hd` instances reported `"shapes": 10`. The fix
  multiplies each cell definition's own-shape count by its total placement
  multiplicity across all instantiation paths (multiplicities compose
  multiplicatively for nested placement — a cell placed inside a cell that
  is itself placed multiple times gets the product, not the sum), via a
  top-down hierarchy walk (`Layout.each_cell_top_down()`/`Instance.size()`)
  rather than a full recursive shape flatten. Only `klt precheck`'s own
  `schema_version` moved to `2`; every other command's `schema_version` and
  `klt --version` (still `0.1.0`) are unaffected — per-command versioning
  per `docs/json-contract.md`. See `docs/cli/precheck.md`.
- 2026-08-03 — `klt eval`: `synthesize` and `place-and-route` are now
  first-class gate `check`s (#437, Phase 5 of Epic #391), joining
  `drc`/`lvs`/`sim`/`layout-metrics`/`functional-verification`. Both always
  report `"status": "ok"` (see `docs/cli/synthesize.md`/
  `docs/cli/place-and-route.md` — a run either produces its output or
  raises), so — like `layout-metrics` — a gate naming either one must
  declare an explicit `threshold` to derive pass/fail; `request` resolves
  as a path only (never inline JSON/`-`), matching each verb's own
  `load_request`. This is what lets a digital candidate's descriptor chain
  `synthesize` -> `functional-verification` -> `place-and-route` ->
  `drc`/`layout-metrics` (over the P&R-produced GDS) into the same single
  `valid`/`objective`/`metrics` verdict an analog descriptor already
  produces, and record it as one `klt trajectory` log entry via the same
  `gates`/`objective` shape. No schema change to `klt eval`'s own response
  envelope. See `docs/cli/eval.md`.
- 2026-08-03 — `klt trajectory` + `klt eval`: a scored evaluation can now be
  recorded to a trajectory log without hand-rolling the record (#437, Phase 5
  of Epic #391 — the trajectory-log half of that issue, completing the gate
  wiring above). New `klayout_tools.trajectory.record_from_eval`/
  `append_record` build one trajectory record straight from a `klt eval`
  envelope's own `objective`/`gates` (collapsing `gates[]` to the record
  schema's lighter `gate_results`) and append it to an append-only JSONL log,
  creating the file and any missing parent directories on first write. Both
  are check-name-agnostic — they read only `objective`/`gates`, so an analog
  envelope (`drc`/`lvs`/`sim`/`layout-metrics`) and a digital one
  (`synthesize`/`functional-verification`/`place-and-route`) log identically.
  `klt eval` gains `--trajectory-log`/`--turn`/`--candidate-ref`/
  `--description`/`--wall-clock-s`, which call that pair as a side effect on
  top of the unchanged response envelope — so one invocation can score a
  candidate *and* log the turn, matching `klt trajectory --plot`'s existing
  "write a file as a courtesy, independent of `--format`" precedent.
  `--trajectory-log` without both `--turn` and `--candidate-ref`, and a
  log-append failure, are both application errors (exit `1`). See
  `docs/cli/eval.md`'s "Trajectory logging" section and
  `docs/cli/trajectory.md`'s "Building a record from `klt eval`" section.
- 2026-08-03 — `klt functional-verification`: new verb (#422, Phase 3 of
  Epic #391). Runs a cocotb testbench against RTL sources through Icarus
  Verilog (default) or Verilator, reporting `status`
  (`"pass"`/`"fail"`), `test_count`/`passed_count`/`failed_count`/
  `skipped_count`, a per-test `tests[]` array (with `error_type`/
  `error_message` on failures), optional Verilator `coverage`
  (`line_pct`/`toggle_pct`/`branch_pct`/`expr_pct` plus an lcov `info_path`),
  and an `environment` reproducibility block. Invoked exclusively through
  cocotb 2.0's first-party Python `Runner` API — never a generated
  Makefile — and the verdict is always derived from the run's own
  `results.xml`, never from a simulator's exit code (which the Phase 1
  survey observed varying between `0`, `1`, and `2` for the *same* failing
  regression). Exit codes reuse `klt lvs`'s `0`/`1`/`2`/`3` trichotomy, so
  `status: "pass"` → `valid: true` and `status: "fail"` → `valid: false` at
  the `klt eval` boundary; `functional-verification` is also now a
  first-class `klt eval` gate `check`. cocotb is an optional runtime
  dependency (not pinned in `pyproject.toml` — cocotb 2.0 caps at Python
  3.13 while `klt` supports 3.10+). See
  `docs/cli/functional-verification.md` and
  `docs/design/cocotb-verification-spike.md` section 7 for the full
  contract.
- 2026-08-03 — `klt functional-verification`: `options.random_seed` (issue
  #423). Pinned to `Runner.test()`'s own `seed` parameter
  (`COCOTB_RANDOM_SEED`) when given; the effective seed cocotb actually used
  (pinned or its own generated value) is always echoed back in
  `environment.random_seed`, read from `results.xml`'s own
  `<property name="random_seed">` — the same reproducibility bar `klt sim`'s
  Monte Carlo seeding and `klt lvs`'s `environment` hashes already set. CI
  now also provisions pinned, checksum-verified Icarus Verilog/Verilator
  builds and the pinned cocotb extra (`scripts/install-icarus-verilog.sh`,
  `scripts/install-verilator.sh`, `pyproject.toml`'s
  `functional-verification` extra), so the GCD worked example
  (`docs/design/cocotb-verification-spike.md` section 6) runs for real in CI
  against both `engine: "icarus"` and `engine: "verilator"` rather than only
  locally.
- 2026-08-03 — `klt place-and-route`: new verb (#425, Phase 4 of Epic #391).
  Places and routes a gate-level netlist (`klt synthesize`'s own
  `netlist_path` output) against a resolved sky130 standard-cell LEF/liberty
  deck via OpenROAD's native Tcl API, one subprocess per stage
  (`floorplan` -> `place` -> `cts` -> `route`), chained via `write_db`/
  `read_db` ODB checkpoints, with `-metrics <file>.json` as the structured
  per-stage metrics channel (confirmed end-to-end against a real
  `openroad/orfs` run for this issue's own worked example). Supports all
  three non-padframe floorplan methods (`utilization`/`explicit`/`def`);
  `target_stage` makes a partial run (e.g. `"place"`) a normal, successful
  outcome with `def_path`/`gds_path` both `null` by design. `def_path` is
  populated once `write_def` has run; `gds_path` only once the DEF is
  merged with the resolved standard-cell GDS view via KLayout's `pya`, in
  -process (never a `klayout` subprocess) — ported from ORFS's own
  `def2stream.py`. Adds a LEF resolver (`klayout_tools.pdk.lef_files()`)
  alongside the existing liberty resolution. `seed` is a required request
  field, echoed unchanged in the response. See `docs/cli/place-and-route.md`
  and `docs/design/digital-flow-contracts-spike.md` section 5 for the full
  contract.
- 2026-08-03 — `klt synthesize`: new verb (#416, Phase 2 of Epic #391).
  Synthesizes RTL sources against a resolved sky130 standard-cell liberty
  via Yosys + bundled ABC (`read_verilog` -> `hierarchy` -> `synth` ->
  `dfflibmap` -> `abc -liberty` -> `clean` -> `stat`/`write_verilog`,
  generated into a debuggable `.ys` script kept alongside the mapped
  netlist), reporting `instance_count`/`area_um2`/`sequential_area_um2`/
  `instance_counts_by_type` parsed from Yosys's own `stat -liberty ...
  -json` output. `pdk.cell_library`/`corner` resolve to a liberty file via
  the same `find_pdk()`/`libs_ref` discovery `klt pdk`/`klt cells` already
  use — no new PDK-fetch mechanism. `timing` is always `null` in this
  contract, deferred to a future OpenROAD/OpenSTA place-and-route phase.
  See `docs/cli/synthesize.md` and
  `docs/design/digital-flow-contracts-spike.md` section 4 for the full
  contract.
- 2026-08-03 — `klt trajectory`: new verb (#388). Renders an append-only
  JSONL optimization-trajectory log (one record per evaluation: `turn`,
  `candidate_ref`, `objective` `{name, value, polarity}`, optional
  `gate_results`/`wall_clock_s`) into a markdown milestone table plus a
  self-contained objective-vs-turn SVG plot for a block repo's README.
  Milestones are the turns where the objective improves on the best-prior
  record by more than a configurable `--threshold`. Operates purely on the
  JSONL file — no live optimizer required — so a hand-written or
  human-curated log renders identically. The record schema mirrors the
  planned `klt eval` envelope's `objective`/`gate_results` shape (#387). See
  `docs/cli/trajectory.md`.
- 2026-08-02 — `klt lvs`: new `device.body_unverified` mismatch category
  (`a483ed0`, #281/#285). Warns (`severity: "warning"`, never changes
  `status`) when a MOS body terminal was extracted onto a deck-synthesized
  net rather than a real drawn tap/well-label net — an NMOS entry fires on
  every inline-extraction LVS run with one or more NMOS devices (no curated
  deck draws a distinct NMOS substrate/tap layer), and a PMOS entry
  additionally fires for decks with no distinct well-tap layer (gf180mcu
  today). This is purely additive (no `schema_version` bump) but changes
  `category_counts` for any gf180mcu (and, for the NMOS case, sky130)
  inline-extraction fixture that previously reported an empty
  `category_counts: {}` — see `docs/cli/lvs.md`'s `device.body_unverified`
  subsection for the full trigger conditions.
- 2026-08-02 — `klt lvs`: new top-level `net_correspondence[]` response
  field (#311). Lists every layout↔reference net pairing the comparer
  matched — unambiguous and ambiguously-resolved alike — as `{layout,
  reference, pin}` entries, sorted and deduplicated per circuit scope so
  `len(net_correspondence) == counts.nets.matched` holds even across a
  hierarchy with cross-circuit net-name collisions. Purely additive (no
  `schema_version` bump) — see `docs/cli/lvs.md`'s `net_correspondence[]
  entries` subsection.
- 2026-08-02 — `klt extract`: the "unmodelled device geometry" diagnostic
  (#288/#299) no longer flags a recognised drawn resistor's own terminal
  head (#324) — a poly component abutting a body region `_resolve_resistors`
  already recognised is now excluded outright, the same way a real MOS gate
  already was, removing a false positive that previously fired on any
  resistor whose wide terminal head carries an ordinary (2+) contact array.
  New top-level `unmodelled_poly[]` response field lists the bounding
  box + `reason` (`"unmarked"` / `"marked_unrecognised"`) of every shape the
  diagnostic still flags, alongside the existing prose `warnings[]` strings.
  Purely additive (no `schema_version` bump) — see `docs/cli/extract.md`'s
  "Known limitation: unmodelled device geometry" subsection. Ordinary poly
  routing tracks sharing the same resistor-body signature remain a known,
  documented false-positive class with a client-side filtering workaround
  via `unmodelled_poly[]`.
- 2026-08-02 — `klt sim`: new optional request block `monte_carlo` (#348,
  phase 1 of #344's decomposition) — re-runs each expanded corner point
  `n` times with a reproducible seed (`monte_carlo.seed`), standing in for
  the per-instance device variation a mismatch-aware model library's
  behavioral parameters draw on. Adds `environment.monte_carlo` (echoes
  `n`/`seed`/`vary`) and a per-sample `corners[].monte_carlo` field
  (`{sample_index, seed, process_seed, mismatch_seed}`, `null` for a
  non-sampled corner); a sample's `corner_id`/artifact path gets a
  `/mc<sample_index>` suffix so per-sample logs never collide. Ships the
  deterministic negative control two public canary repos already rely on
  in their own hand-rolled orchestration: the seed component for whichever
  axis `monte_carlo.vary` does *not* request stays identical across every
  sample of a corner (sigma=0), proving the sampler isn't silently
  injecting or dropping variation. Purely additive (no `schema_version`
  bump), reuses the existing `local`/`local-parallel`/`remote` backends
  unchanged — see `docs/cli/sim.md`'s "Monte Carlo sampling" section.
  Statistics rollup and limit-window evaluation across a sample set landed
  separately as phase 2 (#349, below).
- 2026-08-02 — `klt lvs`: new accepted `request.engine` value `"netgen"`
  (#343) — a second, independent comparator behind the same request/response
  contract, wrapping the open-flow standard
  [`netgen`](https://github.com/RTimothyEdwards/netgen) as a subprocess
  (`netgen -batch lvs`). **Netlist-vs-netlist only** — no `magic` extraction
  backend; it compares the same layout/reference SPICE netlists the
  `"klayout"` engine already resolves, so it validates comparator/contract
  independence, not extraction independence (see `docs/cli/lvs.md` →
  "Engine"). Adds a new `mismatches[].details` field (object | `null`) for
  engine-specific data that does not map onto
  `category`/`net`/`device`/`property` — present and `null`-valued on every
  `"klayout"`-engine entry too, so no entry shape changed — plus two
  netgen-only request options (`options.netgen_setup`,
  `options.netgen_timeout_s`). `environment.engine_version` is netgen's own
  banner-reported version for this engine. Known, documented gap:
  `counts.*.matched` is exact on a `"match"` verdict and `0` on a
  `"mismatch"` verdict, and `net_correspondence` is always `[]`, for
  `"engine": "netgen"` only. Purely additive (no `schema_version` bump); the
  default engine is still `"klayout"` and its output is unchanged apart from
  the new `null` `details` key. Findings (netgen invocation quirks, report-
  format stability) are written up in
  `docs/design/lvs-extraction-spike.md`'s 2026-08-02 addendum.
- 2026-08-02 — `klt sim`: Monte Carlo statistics rollup (#349, phase 2 of
  #344's decomposition). A measurement that ran under `monte_carlo` now
  carries an additive `measurements[].monte_carlo` block —
  `{n, errored, mean, stddev, min, max, quantiles, sigma_window,
  by_corner}` — so callers no longer reduce the raw per-sample corner list
  themselves. `stddev` is the sample (n-1) standard deviation and is `null`
  for `n < 2` rather than a fabricated `0.0`; `quantiles` defaults to
  `[5, 50, 95]` and is configurable via the new `monte_carlo.quantiles`
  request field. The new `monte_carlo.k_sigma` (overridable per measurement
  with `measurements[].k_sigma`) opts into a `mean ± k*stddev`
  limit-window check; both endpoints are scored through the existing
  `_evaluate_limits`, so min/max handling and the margin sign convention
  are shared with the path a single deterministic value takes —
  **a failing window makes the run `fail` (exit `3`) even when every
  individual sample passed its limits**. Without a declared `k_sigma`,
  pass/fail behavior is unchanged; without `monte_carlo`, the response
  shape is unchanged (no `schema_version` bump) — see `docs/cli/sim.md`'s
  "Monte Carlo statistics" section.
- 2026-08-02 — `klt sim`: new `environment.monte_carlo.family_mismatch[]`
  response field (`85faf9d`, #365) — per-device-family mismatch-section
  availability for the selected model library, so a Monte Carlo consumer
  can detect which families actually sampled variation. Purely additive
  (no `schema_version` bump), but changes `environment` contents for every
  gf180mcu Monte Carlo run — see `docs/cli/sim.md`'s
  `environment.monte_carlo` subsection.
- 2026-08-03 — `klt lef-abstract`: new verb (#438, Epic #393 Phase 2
  Capability A). Emits a LEF abstract (`MACRO` block with `PIN`/`OBS`
  sections) from a block's GDSII/OASIS layout plus its `klt socket-check`
  descriptor, so OpenROAD can place it as a hard macro. Pin geometry is
  real drawn metal when present, else a synthesized placeholder box
  (reported per pin as `geometry_source`); obstruction geometry is every
  routing-layer shape not already claimed by a declared pin. Backed by
  `klayout_tools.lef_header`, a new dependency-free tech-LEF header reader
  (`SITE`, routing-layer `PITCH`/`OFFSET`/`DIRECTION`, macro `PIN`
  `DIRECTION`/`USE`) — the deferred-trigger resolution
  `docs/design/sc-leflib-evaluation.md` called out. `docs/schemas/socket
  .schema.json` gains two new optional, additive `pins[]` fields
  (`direction`/`use`, LEF's own vocabulary) this command consumes when
  present. See `docs/cli/lef-abstract.md` and
  `docs/cli/socket-check.md`'s new "LEF translation" section.
- 2026-08-03 — `klt place-and-route`: new `request.macros` field (#438),
  closing the "macro placement" gap the verb's own v1 docstring originally
  scoped out. Each entry fixes one hard-macro instance (e.g. a `klt
  lef-abstract` LEF) at a caller-given location during the floorplan stage
  via OpenROAD's own `place_macro -location ... -orientation ... -exact`
  (never the automatic macro placer). The DEF→GDS merge tolerates an
  abstract-only macro instance (no declared `gds`) staying empty instead of
  raising; a declared `gds` merges the macro's own view in, same as the
  standard-cell GDS merge. Purely additive (`[]`/omitted unchanged; no
  `schema_version` bump) — see `docs/cli/place-and-route.md`'s new
  "Hard-macro placement" section.
- 2026-08-04 — Epic #393 Phase 3 (cross-domain signoff, #456): `klt drc`,
  `klt lvs`, and `klt extract` verified against a real mixed
  sky130_fd_sc_hd + analog-macro layout (a `klt gen diff_pair` block placed
  via `klt place-and-route`'s `request.macros` alongside real standard
  cells, real Yosys + real OpenROAD). No code changes were needed in any of
  the three verbs — each already spans both domains by construction (`klt
  drc`/`klt extract`'s whole-layout flattening has no region concept to
  scope by; `klt lvs`'s comparator has no macro-boundary special case).
  Findings and the injected-violation/corrupted-reference verification
  methodology are recorded in `docs/cli/drc.md`, `docs/cli/lvs.md`, and
  `docs/cli/extract.md`'s new "Mixed sky130_fd_sc_hd + analog-macro
  layout"/"...netlist" sections. One real domain-boundary gap was found and
  filed separately (not fixed here, per this phase's own decomposition
  precedent, #451/#452): #464, a `klt lef-abstract` macro pin with no
  `PORT` geometry that, if wired into a real net, fails `klt
  place-and-route` opaquely (OpenROAD `GRT-0029`) rather than with a clear
  `klt`-level error. This is a documentation/verification-only change — no
  `schema_version` bump for any of the three verbs.
