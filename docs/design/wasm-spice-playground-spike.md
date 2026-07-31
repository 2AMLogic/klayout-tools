# Spike: in-browser SPICE playground (WASM ngspice) engine survey

**Status:** spike / proposal. Nothing here is scheduled, and nothing here
authorises implementation. Per
[docs/ARCHITECTURE.md](../ARCHITECTURE.md) → "How capabilities arrive," a
major capability arrives by spiking a design epic first — candidate-engine
survey, license findings, and a wrap/build decision — filed before
implementation starts. This is that spike for Epic #90 Phase 3 (issue
#139): can a gallery visitor edit stimulus and re-run real SPICE
client-side, with no backend?

**Findings summary, ahead of the detail below:** yes, with a scoped build.
A mature, actively maintained, MIT-licensed WASM ngspice build exists
(`eecircuit-engine`), its underlying ngspice is the same BSD-3-lineage
engine [docs/design/spice-corner-runner-spike.md](spice-corner-runner-spike.md)
already accepted for `klt sim`, and independently measured perf on a
gallery-block-scale netlist is sub-second per re-run. The model-deck size
problem the epic's Risks section calls out is real but **already solved
at build time** by this repo's own gallery signals pipeline
(`scripts/gallery_signals.py`, `scripts/fetch-cell-netlists.sh` — issue
#99) — the same per-cell trimming approach ports directly to the browser.
Recommendation: **proceed to a scoped playground build**, not a wrap-at-
Phase-2 stop. See "Wrap or build?" for the full reasoning and the
recommended scope boundary.

## 1. Candidate-engine survey

### eecircuit-engine (recommended)

| Property | Finding |
| -------- | ------- |
| Upstream | [eelab-dev/EEcircuit-engine](https://github.com/eelab-dev/EEcircuit-engine), the simulation engine behind [EEcircuit](https://github.com/eelab-dev/EEcircuit) (177 stars) / [EEsim.dev](https://eesim.dev). Published to npm as [`eecircuit-engine`](https://www.npmjs.com/package/eecircuit-engine). |
| Maintenance | Active as of this spike: last push 2026-04-07 (repo) / 2026-04-05 (engine package), 73 published versions, 22,118 npm downloads in the trailing month (measured 2026-07-31 via `api.npmjs.org/downloads/point/last-month`), 0 open issues on the engine repo. Consumed downstream by at least one third party (`tscircuit/ngspice-spice-engine`), a real adoption signal beyond the origin project. |
| License | Wrapper/JS/build scripts: MIT (`LICENSE` file, verified). Compiled core: ngspice itself, built from [`danchitnis/ngspice-sf-mirror`](https://github.com/danchitnis/ngspice-sf-mirror) (a mirror of the same upstream ngspice project already surveyed and accepted in the corner-runner spike) — BSD-3-Clause lineage, same as `klt sim`'s engine. The build (`Docker/run.sh`) configures with **`--disable-xspice --disable-osdi`** and no `--enable-klu`, which sidesteps both license questions the corner-runner spike flagged as unresolved (XSPICE's separate terms, KLU/SuiteSparse LGPL) — this WASM build carries a narrower, cleaner license surface than a full-featured ngspice binary, not a wider one. |
| Bundle composition & size | Downloaded and inspected the published npm tarball directly (`eecircuit-engine@1.7.0`, 2026): `unpackedSize` 40.7 MB; the actual runtime artifact is one of two equivalent bundles (`dist/eecircuit-engine.mjs` or `.umd.js`), each ~19 MB raw, ~**5.75 MB gzip-over-the-wire** (measured with `gzip -c | wc -c`). The WASM binary is base64-inlined into the JS (Emscripten `instantiateWasm`/`WebAssembly.instantiate`, confirmed by grep) rather than shipped as a separate `.wasm` fetch — a packaging detail that matters for the lazy-load recommendation below (the whole engine is one atomic module, not splittable at import time). |
| Bundled model data | The npm package embeds a **pre-trimmed** subset of open-PDK device models as TS string constants under `src/models/`: sky130 (`nfet18.ts` 469 KB, `pfet18.ts` 516 KB, `pfet18_hvt.ts` 1.30 MB — i.e. exactly the two primitives, `nfet_01v8`/`pfet_01v8_hvt`, this repo's own gallery cells instantiate) and gf180mcu (`gf180mos.ts` 1.30 MB, covering the full device list in the repo's `GF180.md`, including `nmos_6p0`/`pmos_6p0` — confirmed present by grep — the exact substitute devices `scripts/gallery_signals.py` already maps gf180mcu's `nfet_05v0`/`pfet_05v0` onto). Every model file carries the original Apache-2.0 header with attribution intact (`Copyright 2020 The SkyWater PDK Authors` / `Copyright 2022 GlobalFoundries PDK Authors`, verified by fetching the raw files) — the same open-PDK sources (`skywater-pdk-libs-sky130_fd_pr`, `globalfoundries-pdk-libs-gf180mcu_fd_pr`) this repo's own `scripts/fetch-cell-netlists.sh` vendors. Total embedded model text across both PDKs: ~3.8 MB — small relative to the 19 MB bundle, which is dominated by the compiled ngspice binary itself (compact device models like BSIM3/4 are inherently large in *code*, not just model-card data; that part doesn't shrink by trimming decks). |
| Headless / environment fit | Built with Emscripten `MODULARIZE=1 EXPORT_ES6=1 ENVIRONMENT="web,worker"`, targeting browser + worker contexts — the right shape for a site-side playground (can run in a Web Worker off the main thread). Also runs under plain Node via `tsx` (the package's own `test/test-package.ts` does exactly this with a dynamic `import()`), which is what let this spike measure real performance directly (below) without standing up a browser harness. |
| API shape | `new Simulation()` → `await sim.start()` (one-time WASM init) → `sim.setNetList(text)` → `await sim.runSim()` → structured result (`{ header, numVariables, variableNames, numPoints, dataType, data: [{name, type, values}] }` per trace) — already JSON-shaped, no log-scraping required for the transient-waveform case this playground needs (in contrast to the corner-runner spike's `.meas`-scraping problem, which was about scalar measurements from batch-mode log files; this API returns vectors directly from the in-process engine). |

### wokwi/ngspice-wasm (predecessor, superseded)

| Property | Finding |
| -------- | ------- |
| Upstream | [wokwi/ngspice-wasm](https://github.com/wokwi/ngspice-wasm) — its own README states it is "based on EEsim" (the same lineage as `eecircuit-engine` above). |
| Maintenance | Last push 2022-11-01 — over 3 years stale as of this spike (2026-07-31), 9 stars, no npm package. |
| Verdict | Same underlying approach, strictly worse maintenance status. Not a competing option, historical predecessor to the recommended candidate. |

### Other repos surfaced, set aside

A GitHub code-search sweep (`ngspice wasm`, `spice simulator wasm
topic:webassembly`, `eecircuit`) surfaced a long tail of personal forks and
zero/low-star derivatives of `EEcircuit`/`EEcircuit-engine` (`z-wasm/*`,
`aminemahd13/*`, `ksalmon1/*`, `DaCameraGirl/voltline`,
`SinDeployNoHayPizza/plaketia`, `shishir-dey/ngspiceX`) — all either
consume `eecircuit-engine` as a dependency or reimplement the same
build-ngspice-with-Emscripten approach at a fraction of the stars/activity.
None represents an independent engine choice; they corroborate that
`eecircuit-engine` is the de facto WASM-ngspice distribution the field has
converged on, not one option among several equally mature ones.

**Xyce, Spectre/HSPICE, and a from-scratch Rust/Zig rewrite** were not
resurveyed here — the corner-runner spike already ruled out Xyce (open-PDK
fit: sky130's native decks and every open-source flow that touches them
are ngspice-shaped) and proprietary engines (license), and nothing in this
spike's scope changes either conclusion for a browser target. A
from-scratch rewrite fails the same "Rewrite rule" test the corner-runner
spike already applied to numerics/device models (below) — no reason to
re-run that analysis for a WASM target specifically.

### Recommendation

**`eecircuit-engine`.** It is the only actively maintained, licensed,
adoption-evidenced candidate; it wraps the exact ngspice lineage `klt sim`
already commits to; and (unexpectedly, this is the spike's most useful
finding) its own bundled model data independently validates the trimming
strategy this repo already built for build-time simulation.

## 2. License findings

- **Wrapper/engine code**: MIT (`eecircuit-engine`'s own `LICENSE`,
  verified by direct fetch). Freely redistributable, including as a
  bundled dependency of this repo's site build.
- **Compiled ngspice core**: BSD-3-Clause lineage, same conclusion the
  corner-runner spike already reached for the CLI engine — and this build
  configuration is *more* conservative than a stock ngspice build: XSPICE
  (separate license terms) and OSDI are compiled out, and KLU/SuiteSparse
  (LGPL) is never enabled. There is no license question here that the
  already-accepted corner-runner spike didn't already resolve for the
  identical upstream project.
- **Bundled PDK model data**: Apache-2.0, from the same two upstream
  repos (`google/skywater-pdk-libs-sky130_fd_pr`,
  `google/globalfoundries-pdk-libs-gf180mcu_fd_pr`) this repo's own
  `scripts/fetch-cell-netlists.sh` already vendors and checksums for the
  build-time gallery signals pipeline. Attribution headers are preserved
  verbatim in the embedded source (verified). One minor compliance gap
  worth fixing *before* any build lands: `eecircuit-engine`'s top-level
  `LICENSE` file only states MIT and does not itself list the Apache-2.0
  model-data attributions (they live in the source comments instead of a
  `NOTICE`-style rollup) — not a blocker, since Apache-2.0's requirement is
  attribution-preservation in the redistributed file, which is satisfied,
  but a follow-up build should carry the same attribution forward in
  whatever form this repo lands the model text (matching how
  `scripts/fetch-cell-netlists.sh`'s own header block already documents
  its sources).
- **Redistribution of *our* PDK data through this path**: if the recommended
  architecture (§3) supplies our own vendored, checksum-verified model
  files instead of trusting the engine's baked-in copy, the same
  Apache-2.0 terms apply identically — no new license obligation is
  introduced by shipping them to a browser instead of a CI build machine.
  Nothing proprietary is involved anywhere in this chain — sky130 and
  gf180mcu are the two open PDKs the repo already only uses.

## 3. Model-deck size / trimming strategy

The epic's Risks section states plainly: "full PDK decks are tens of MB;
the spike must propose trimming/lazy-loading before any playground
build." Two independent measurements confirm the scale of the problem and
that trimming solves it completely:

- **Untrimmed baseline**: the full `sky130_fd_pr` primitive-device SPICE
  library (every device, every corner) is **48 MB** (measured directly:
  `libs.ref/sky130_fd_pr/spice/` in a local volare sky130A checkout). The
  full PDK variant tree (GDS, tech files, everything) is 1.2 GB — the "tens
  of MB" the epic worries about is, if anything, an undercount for the
  worst case (whole-PDK) and an overcount for the actually-needed case
  (below).
- **Trimmed, per-cell baseline (already built and shipping today)**:
  `scripts/gallery_signals.py`'s `_sky130_models_lib` assembles a
  `tt`/`ss`/`ff` model library from exactly the two device-corner files the
  4 sky130 gallery cells instantiate (`nfet_01v8`, `pfet_01v8_hvt`) —
  measured at **~470 KB** for the 3-corner set (2 devices × 3 corner files,
  68–88 KB each, plus ~4 KB mismatch files), a **>100x** reduction from the
  48 MB baseline, with zero loss of fidelity for the cells that need it.
  This code already exists in this repo and already runs in CI (issue
  #99); it is not a proposal, it is prior art.
- **eecircuit-engine's own trimming, independently arrived at**: the
  candidate engine's bundled sky130 model set (`nfet18.ts` + `pfet18.ts` +
  `pfet18_hvt.ts`) totals **~2.3 MB** — same two devices (plus a `pfet18`
  non-hvt variant this repo's cells don't use), same order of magnitude as
  this repo's own trimmed set, arrived at independently by a different
  project applying the same logic (only ship what's instantiated). This is
  the strongest evidence in this spike that the trimming approach is sound,
  not merely convenient: two unrelated projects converged on it.

**Proposed strategy for a playground build** (not built in this spike):

1. **Reuse, don't trust, the engine's baked-in models.** Rather than
   depending on `eecircuit-engine`'s embedded `nfet18.ts`/`gf180mos.ts`
   blobs (which can silently drift from upstream between engine releases,
   with no checksum tying them to a specific PDK commit), call
   `sim.setNetList()` with a netlist body that includes our own
   `pdks/cell-netlists/<pdk>/models/*` text — the exact files
   `scripts/fetch-cell-netlists.sh` already fetches and checksums for
   build-time `klt sim` runs. Provenance stays in one place
   (`environment`-hashed, matching `docs/cli/sim.md`'s reproducibility
   guarantees) instead of splitting between "what CI simulated" and "what
   the browser simulated."
2. **Ship the trimmed model text as a static site asset, not inline in the
   JS bundle.** `site/scripts/copy-renders.mjs` already stages per-block
   artifacts (`renders`, and `signals` per issue #99/#100) from
   `blocks/<slug>/output/` into `site/public/blocks/<slug>/...`; the same
   mechanism can stage a `models.spice` (or per-corner variant) file
   alongside a block's existing `signals/` artifacts — reusing an
   established pipeline seam rather than inventing a new one.
3. **Lazy-load the engine itself, on interaction, not on page load.** The
   5.75 MB gzip payload is a real cost, but it is a *fetch-when-clicked*
   cost, not a *first-paint* cost: `import("eecircuit-engine")` behind a
   "make stimulus editable" affordance on the block detail page, exactly
   the pattern the existing waveform viewer (issue #100,
   `site/src/components/waveform/`) already gates its own optional
   section behind (`layout.signals !== undefined`). No visitor who never
   touches the playground control pays the transfer cost.
4. **One PDK, one engine instance, cached across corner/stimulus edits.**
   `sim.start()` is the one-time ~600 ms cost (measured below); it should
   run once per page visit to the playground, not once per re-run.

## 4. Perf on standard cells

Measured directly (not estimated) by installing `eecircuit-engine@1.7.0`
from npm and running it under Node via `tsx`-equivalent dynamic import —
the same harness the package's own `test/test-package.ts` uses, so this is
exercising the real compiled WASM binary, not a mock. Netlist: a
transistor-level sky130 inverter (`sky130_fd_pr__nfet_01v8` /
`sky130_fd_pr__pfet_01v8_hvt`, matching this repo's own `comb1` gallery
cell family — `inv_1`, `buf_4`, `clkinv_1`), 40 ns transient with a 10 ps
rise/fall pulse, `.tran 10p 40n` (4,060 output points).

| Operation | Measured time |
| --------- | ------------- |
| Engine start (`sim.start()`, one-time WASM instantiation) | ~630 ms |
| First `runSim()` (inverter transient) | ~888 ms |
| Second `runSim()` on the same engine instance (simulating "edit stimulus, click re-run") | ~574 ms |
| Third `runSim()`, a 2-stage gate chain (comb1 + comb1, closer to the `dff`/`comb2` gallery families' complexity), across 3 process corners in sequence | 1023 ms / 737 ms / 671 ms |

Caveats, stated plainly: this ran in Node (V8), not a browser tab — real
browser WASM performance is generally comparable or somewhat slower
depending on JIT warmup and thread/worker overhead this harness doesn't
model, and this spike did not benchmark in an actual browser context
(no browser automation available in this environment; flagged as an open
item below). No corner-matrix parallelism was tested (the browser is
single-threaded per `Simulation` instance; sweeping N corners means N
sequential re-runs, or N engine instances in separate Web Workers).

**Conclusion for the interactivity target**: sub-second to ~1 s per
re-run, after a one-time ~600 ms engine load, is well inside the budget for
an "edit stimulus, click Re-run, see the waveform update" interaction —
not real-time-per-keystroke, but that was never the epic's target (the
epic goal is "visitors edit stimulus and re-run," not live scrubbing).

## 5. Wrap or build?

Applying [docs/ARCHITECTURE.md](../ARCHITECTURE.md) → "Rewrite rule" the
same way the corner-runner spike did, but to the *playground* capability
(not the numerics, which that spike already settled):

1. **Bottleneck or ceiling — fails, for the numerics.** Nothing here
   argues for reimplementing SPICE device physics in Rust/WASM from
   scratch; `eecircuit-engine`'s compiled ngspice already exists, is
   maintained, and is licensed compatibly. This is the same conclusion the
   corner-runner spike reached, extended to the browser target.
2. **Oracle exists — holds.** The same ngspice numerics power both `klt
   sim` (server/CI-side, via subprocess) and this candidate (browser-side,
   via WASM) — a playground result should be diffable against a `klt sim`
   run on the identical netlist/model/corner as a correctness check, the
   same guarantee the corner-runner spike's contract already values.
3. **Unlock — holds, for the *product*, not the numerics.** Nothing about
   the numerics is unlocked by going client-side (ngspice is ngspice
   either way). What *is* unlocked is the product capability the epic
   actually wants: a visitor editing stimulus and re-running a real
   transistor-level simulation with no backend round-trip, no server
   fleet to operate, and no per-visitor compute cost to this org. That is
   a real, structural difference from the precomputed-signals viewer
   (issue #100) this epic already shipped, not an incremental improvement
   on it.

**This is not "wrap ngspice" in the sense the corner-runner spike used the
term** (a thin subprocess shell) — it's closer to "adopt a maintained
distribution of the same engine, compiled for a different target," with a
real integration layer above it (model provisioning, lazy-loading,
stimulus-editing UI, waveform re-render) that is genuinely new product
work, not orchestration glue this repo would otherwise have to write
itself from scratch. `eecircuit-engine`'s Simulation API already returns
structured vectors — there is no log-scraping layer to build here the way
there was for `klt sim`'s `.meas` extraction.

**Recommendation: proceed to a scoped playground build**, not a wrap-at-
Phase-2 stop. The reasons a stop was on the table — model-deck size and
engine maturity/license risk — are the two things this spike specifically
resolved favorably: the trimming approach isn't just proposed, it's
already running in this repo's own CI (issue #99) and independently
corroborated by the candidate engine's own bundled model data; the engine
candidate is actively maintained, MIT/BSD-3/Apache-2.0 clean, and measured
fast enough for the interaction the epic describes.

### Recommended scope boundary for the follow-up build (not this spike)

To keep the first playground build small and land it as a bounded epic
phase rather than open-ended scope:

- **Which blocks**: start with the 4 sky130 gallery cells (this repo's own
  vendored model subset is the most complete, and sky130 is this repo's
  first-class PDK per `CLAUDE.md`); gf180mcu can follow once the sky130
  path is proven, reusing the same device-substitution mapping
  `scripts/gallery_signals.py` already documents.
- **What's editable**: stimulus parameters the existing testbench
  generator already parameterizes (pulse timing/period/edge rates,
  supply value, temperature/corner selection) — not free-form netlist
  text. Free-form editing reopens the "what if a visitor's netlist
  references a device we haven't vendored" problem this spike's trimming
  strategy is specifically designed to close off.
- **What's not in scope yet**: waveform-vs-`klt sim` cross-validation in
  CI (a good idea, tracked as an open question below, not a blocker to a
  first build); true corner-matrix sweeping client-side (sequential
  re-runs are fine for v1; parallel Web Workers are a later optimization);
  gf180mcu's five-corner set (`typical`/`ff`/`ss`/`fs`/`sf`) — start with
  the 3-way `tt`/`ss`/`ff` sky130 already uses.

## Out of scope for this spike

No dependency was added to `site/package.json`, no playground component
was written, no `klt` subcommand was touched, and no model-deck asset was
staged into `site/public/`. Those remain the follow-up build epic's scope,
gated on this spike's findings.

## Open questions for a follow-up epic

- **Real-browser perf validation.** This spike's numbers come from a Node
  harness (V8), not an actual browser tab; a follow-up build's first task
  should reproduce the same measurement in a real browser (or at minimum
  in the site's own Vite dev server) before committing to a UX that
  assumes sub-second re-runs.
- **Cross-validation contract.** Whether/how a playground re-run's result
  gets spot-checked against a `klt sim` run on the same inputs (build-time
  regression test, or a periodic CI check) — valuable for catching
  silent WASM-vs-CLI ngspice version drift, not designed in this spike.
- **Engine version pinning.** `eecircuit-engine` tracks whatever ngspice
  tag was latest at its own build time (`build-ngspice.sh` resolves "the
  latest release version" dynamically); a production integration should
  pin an exact `eecircuit-engine` npm version and record the ngspice
  version it embeds (available at runtime via the result header's
  `Command: ngspice-45.2+, Build ...` line, confirmed present in this
  spike's test output) the same way `klt sim`'s `environment.engine_version`
  field already does server-side.
- **Worker placement.** Whether the engine runs on the main thread (simpler,
  UI-jank risk on a ~600 ms-1 s run) or a dedicated Web Worker (the
  Emscripten build already targets `"web,worker"`, so this is supported,
  not a rebuild) — a UX call for the follow-up build, not this spike.
- **gf180mcu five-corner coverage** and whether the device-substitution
  documented in `scripts/gallery_signals.py` needs any adjustment for a
  visitor-editable context (it currently only needs to be correct for the
  3 fixed testbenches that module generates at build time; a playground
  that lets a visitor pick different rail/pin combinations may exercise
  substitution paths that module never has to).
