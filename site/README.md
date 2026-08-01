# klayout-tools.org site

This directory holds the Vite + React project that builds and deploys
klayout-tools.org (Epic #13; migrated from Astro to Vite + React + Tailwind +
shadcn/ui in issue #92, part of Epic #90's Phase 1). It is a **like-for-like
replatform**: same URLs, same content, same dark theme, same static-deploy
story — the stack underneath changed so later Epic #90 phases (a waveform
viewer, an in-browser SPICE playground) have a React-based UI to build on.

## Build and deploy

[`../scripts/deploy-site.sh`](../scripts/deploy-site.sh) builds this project
(`npm --prefix site ci && npm --prefix site run build`, producing
`site/dist/`) and deploys `site/dist/` to Cloudflare Pages (project
`klayout-tools`, custom domain klayout-tools.org). Auth uses a scoped API
token, not wrangler OAuth — see [`scripts/README.md`](../scripts/README.md)
for the auth flow.

```
source ~/.cloudflare/rjwalters/pages-rjwalters.env
scripts/deploy-site.sh
```

Pass `--no-deploy` to run the build only (verify `site/dist/` locally
without touching Cloudflare):

```
scripts/deploy-site.sh --no-deploy
```

`deploy-site.sh` builds and deploys whatever is already checked into
`blocks/` and `site/` — regenerating `blocks/*/output/layout.json` or
renders is the content pipeline (#62), out of scope for this script.

### Automated deploy (deploy trigger)

The chosen deploy trigger is a **GitHub Actions workflow on push to `main`**
([`../.github/workflows/deploy-site.yml`](../.github/workflows/deploy-site.yml),
issue #166). It is path-filtered to `site/**` and `blocks/**` (plus the deploy
script and the workflow itself), so a merge that changes the site or the
gallery data runs `scripts/deploy-site.sh` automatically — a merge that touches
neither skips it. This closes the gap where content merged to `main` but never
reached the live site until someone ran the deploy by hand.

Deploys are serialized by a `concurrency` group with `cancel-in-progress:
false`: two merges in quick succession **queue** rather than clobber each other
(the "last-writer-wins" failure mode), so the live site always ends on fresh
`main`.

**Operator setup required.** The workflow deploys with the same
`CLOUDFLARE_API_TOKEN` / `CLOUDFLARE_ACCOUNT_ID` credentials the script expects,
read from repository secrets. Those secrets must be added by an operator (they
live only on the operator's machine today, in
`~/.cloudflare/rjwalters/pages-rjwalters.env`); until they exist, the workflow
runs but its deploy step fails fast on the script's own
`: "${CLOUDFLARE_API_TOKEN:?...}"` guard — it never deploys to the wrong account
or silently no-ops. Add them with:

```
gh secret set CLOUDFLARE_API_TOKEN
gh secret set CLOUDFLARE_ACCOUNT_ID
```

Until the workflow is live and green, `main` and the deployed site can drift;
run one manual `scripts/deploy-site.sh` (with the env file sourced) to catch the
live site up to `main`.

## Vite + React gallery project

Self-contained npm project: it has its own `package.json` and does **not**
depend on the repository's Python tooling or root `package.json`.

### Quick start

```bash
cd site
npm install      # installs React/Vite/Tailwind/shadcn deps into site/node_modules
npm run dev      # serves the site at http://localhost:5173/ (SSR dev preview, real data)
npm run build    # type-checks, then produces a static prerendered site in site/dist/
npm run preview  # serves the built site/dist/ locally
npm run check    # TypeScript type check only
npm test         # runs the loader/build-info unit tests (vitest)
```

`npm run build` succeeds even on a fresh checkout with **zero**
`layout.json` files present — every block without data renders as a
`status: no_artifacts` card with a placeholder thumbnail.

### Static output, no server runtime

The site is a **fully static** deploy: nothing in `site/dist/` requires a
server process at runtime. This is implemented without Astro's built-in
static-output mode — instead:

- `vite build` produces the hydratable client bundle (`src/entry-client.tsx`
  + hashed assets) under `dist/`.
- A second, throwaway Vite SSR build compiles `src/entry-server.tsx`
  (`renderToString` + the data loaders) to `dist-ssr/` (deleted after use,
  never deployed).
- [`scripts/prerender.mjs`](scripts/prerender.mjs) imports that SSR bundle
  directly in Node, enumerates every route the same way Astro's
  `getStaticPaths()` used to (`/` plus one `/<slug>/` per block from
  `loadLayouts()`), renders each to an HTML string, and writes
  `dist/index.html` / `dist/<slug>/index.html` — the same directory +
  `index.html`-per-route shape the previous Astro build produced.

`npm run dev` reuses the same `src/entry-server.tsx` render functions via a
small SSR middleware in [`vite.config.ts`](vite.config.ts) (`server.
ssrLoadModule`), so the dev server shows real content (with HMR) rather than
an empty shell — it is not a plain client-side-only preview.

### Gallery index

The landing page (`src/pages/IndexPage.tsx`) renders one `LayoutCard`
(`src/components/LayoutCard.tsx`) per block returned by `loadLayouts()`, in
a responsive card grid. Each card shows:

- A thumbnail — the first entry in the block's `renders` map (per-layer
  PNGs from `klt render`, #60), or `public/placeholder-layout.svg` when
  `renders` is absent.
- The block's `name` and `description` (when present).
- Metric badges for `layer_count`, `cell_count`, `instance_count`, and
  `drc.status`/`drc.violation_count` — each omitted when its backing field
  is absent, never rendered as `null`.
- A status chip reflecting `status` (`ok` / `partial` / `no_artifacts`).

Cards link to `/<slug>/` (the per-block detail route).

`renders` values in `layout.json` are paths relative to the block's
`output/` directory (e.g. `"renders/1_0.png"`). Because the static build can
only serve assets under `site/public/`, a `predev`/`prebuild` step (`npm run
copy-renders`, `scripts/copy-renders.mjs` — unchanged by the Astro -> Vite
migration) stages `blocks/<slug>/output/renders/*.png` into
`site/public/blocks/<slug>/renders/` before every `dev`/`build` run. The
staged tree is git-ignored and regenerated each run; a fresh checkout with
no renders is a no-op.

### Layout data

The build-time loader (`src/data/loadLayouts.ts`) discovers block
directories under the repository's `blocks/` tree and reads each block's
`blocks/<slug>/output/layout.json`. Ported unchanged (behavior and tests)
from the pre-migration Astro site — this is the contract with the block
pipeline (#62) and must not change here.

The loader is resilient to missing data:

- **No `layout.json`** → a stub record with `status: "no_artifacts"`.
- **Unknown `schema_version`** → skipped with a warning (a stub is emitted),
  so the build still completes with the remaining blocks.
- **Valid `layout.json`** → parsed into a typed `Layout` (see
  `src/data/types.ts`).

Blocks are discovered as immediate subdirectories of `blocks/`, skipping
hidden / `_`-prefixed entries.

### Block detail pages

`src/pages/DetailPage.tsx` generates a static `/<slug>/` route for every
block `loadLayouts()` discovers, showing its renders, metrics,
name/description, and (once #62's public-repo gate sets `downloadable:
true`) a download link — `no_artifacts` blocks render a clear "not built"
state instead of a broken page. Like the pre-migration `[slug].astro`, this
page does not render the shared `Header`/`Footer` chrome (only the index
page does) — preserved as-is rather than "fixed" as part of a like-for-like
platform migration.

Render images (and, once downloadable, the source layout file) live outside
`site/` under `blocks/<slug>/output/...`; the static build cannot reference
files outside its project root, so the `predev`/`prebuild` npm hooks run
`scripts/copy-renders.mjs` first, which stages them into
`site/public/blocks/<slug>/...` (git-ignored, regenerated every run) so they
are served from `/blocks/<slug>/...`.

### Waveform viewer (Signals section)

When a block's `layout.json` has a `signals` field (see `LayoutSignals` in
`src/data/types.ts`), `DetailPage.tsx` renders a "Signals" section between
Metrics and Downloads, using `src/components/waveform/WaveformViewer.tsx`.
It supports zooming the time axis, placing a click cursor with a
nearest-sample readout, toggling per-node visibility, and selecting/
overlaying multiple PVT corners. Blocks with no `signals` field render
exactly as before (the section is omitted, not shown empty).

Per-corner waveform sample data is never inlined into `layout.json` — each
`signals.corners[]` entry references a `waveform` JSON artifact path
(relative to the block's `output/` directory, matching the same convention
`renders` values use), staged by `copy-renders.mjs` alongside renders and
fetched client-side by the viewer. This mirrors `klt sim`'s own
`artifacts.waveform` contract (`docs/cli/sim.md`) — see the field docs on
`Layout.signals` in `types.ts` for the full shape and its provisional
status pending issue #99 (the signals pipeline that will produce this data
for real).

### SPICE model decks (playground, phase A)

For the in-browser SPICE playground (Epic #90, issue #149 / parent #148), the
`predev`/`prebuild` hooks also run `scripts/stage-models.mjs` (after
`copy-renders.mjs`), which stages self-contained sky130 primitive-device model
decks — one per `tt`/`ss`/`ff` process corner — for the four sky130 gallery
standard cells, at `/blocks/<slug>/models/<corner>.spice`. Each deck is the
same device-corner text `scripts/gallery_signals.py`'s `_sky130_models_lib`
assembles for build-time `klt sim` runs (`nfet_01v8` + `pfet_01v8_hvt`), with
the corner files' relative `.include` of their `*.pm3.spice` binned models
resolved inline so each deck is one portable file a browser SPICE engine can
`fetch()` directly. A `models/checksums.json` publishes each deck's SHA-256
plus the SHA-256 of every `pdks/cell-netlists/` source file it was built from
(the `docs/cli/sim.md` `environment` hashing convention), and a `models/NOTICE`
carries the Apache-2.0 attribution for the SkyWater PDK model text. Source
lives in `pdks/cell-netlists/` (git-ignored, populated by
`scripts/fetch-cell-netlists.sh`); a fresh checkout with no fetched netlists is
a no-op. gf180mcu is out of scope for this phase — sky130 only.

### SPICE playground engine wrapper (phase B)

`src/lib/playground/` is the client-side SPICE engine wrapper (Epic #90, issue
#150 / parent #148) that phase C's playground UI (#151) builds on. It wraps
[`eecircuit-engine`](https://www.npmjs.com/package/eecircuit-engine) — a
MIT-licensed WASM build of ngspice (BSD-3 core) recommended by the spike
(`docs/design/wasm-spice-playground-spike.md`) — with three concerns:

- **`netlist.ts`** (pure, no engine): turns editable stimulus parameters
  (`StimulusParams`: supply, pulse slew/timing, load cap, temperature, transient
  window) plus a phase-A vendored model deck into one self-contained SPICE deck
  via `{{name}}`-placeholder substitution (`assembleNetlist`). The default
  testbench is a transistor-level inverter built from the two vendored sky130
  primitives (`nfet_01v8` / `pfet_01v8_hvt`) — the same netlist the spike
  benchmarked. The model text is **spliced in from the checksummed phase-A deck**
  (`/blocks/<slug>/models/<corner>.spice`), never the engine's bundled model
  blobs. Being engine-free, the parameter → netlist substitution is fully
  unit-tested (`netlist.test.ts`).
- **`engine.ts`**: lazy-loads the ~19 MB (5.75 MB gzip) engine only via a dynamic
  `import("eecircuit-engine")` inside `loadSimulation()` — nothing imports it at
  page load (`DetailPage` does not reference it), so a visitor who never triggers
  the playground never fetches it. `loadSimulation()` memoizes the started
  instance so `sim.start()` (the one-time ~600 ms WASM init) runs **once per page
  visit**, reused across every re-run. `runPlayground()` orchestrates fetch →
  assemble → `setNetList` → `runSim` and returns the result in the site's shared
  `WaveformData` shape.

**Engine version pinning** (spike open question): `eecircuit-engine` is pinned to
an **exact** version in `package.json` (`1.7.0`); the ngspice version it embeds
is recorded in `EMBEDDED_NGSPICE_VERSION` (`ngspice-45.2+`, build
`Tue Mar 24 02:02:56 UTC 2026`) and confirmed at runtime from each result
header by `extractNgspiceVersion()` — the same way `klt sim`'s
`environment.engine_version` records the server-side engine.

**Perf validation** (spike open question, "Real-browser perf validation"):
`npm run playground-perf` reproduces the spike's Node methodology (one cold
`start()`, then two `runSim()` calls on the same instance). Measured on this
build: `start()` ~580–630 ms, re-runs well under the ~1 s budget — matching the
spike's numbers. To validate in a **real browser tab** rather than Node, run the
equivalent `perfProbe()` (exported from `engine.ts`) from the Vite dev server's
DevTools console; the engine's Emscripten build targets `"web,worker"`, so a
future phase may move it to a dedicated Web Worker (a phase-C UX call) without a
rebuild.

### Bootstrap data

The `layout.json` schema is the contract emitted by `klt layout-metrics`
(issue #61, "Gallery: per-layout metrics extractor") — see
[`../docs/cli/layout-metrics.md`](../docs/cli/layout-metrics.md) and
`src/klayout_tools/layout_metrics.py` for the authoritative shape (the
always-present fields are `schema_version`, `generated_at`, `slug`, `name`,
`status`). The checked-in `blocks/` tree is bootstrapped against the [#4
test corpus](../tests/corpus/README.md):
[`../scripts/bootstrap-gallery-blocks.py`](../scripts/bootstrap-gallery-blocks.py)
runs `klt layers` / `klt cells` against each corpus GDS file and writes
`blocks/<slug>/output/layout.json` in that same shape. See
[`../blocks/README.md`](../blocks/README.md) for what is checked in and why
one block is intentionally left without a `layout.json` (to demonstrate the
`no_artifacts` path on the real placeholder page below).

### Tailwind + shadcn/ui

Dark-theme colors live as Tailwind v4 `@theme` tokens in `src/index.css`
(`--color-night`, `--color-panel`, `--color-orange`, `--color-cyan`, etc.) —
the same hex values the pre-migration Astro CSS custom properties used, so
the theme carries over pixel-for-pixel. `src/components/ui/` holds
shadcn/ui-style primitives (`badge.tsx`, `card.tsx`, `table.tsx`) built on
`class-variance-authority` + `tailwind-merge` (`src/lib/utils.ts`'s `cn`
helper); `components.json` documents the shadcn CLI configuration so a
future `npx shadcn add <component>` (e.g. for Phase 2's waveform viewer)
drops new components into `src/components/ui/` following the same pattern.

### Layout

```
site/
  README.md            # this file
  index.html            # Vite app-shell entry (dev CSR fallback + prerender template)
  package.json          # React/Vite/Tailwind/shadcn dependencies (isolated from repo root)
  vite.config.ts        # Vite config: React + Tailwind plugins, dev-time SSR middleware
  components.json       # shadcn/ui CLI configuration
  tsconfig.json / tsconfig.app.json / tsconfig.node.json
  dist/                  # build output (git-ignored, deploy target)
  scripts/
    copy-renders.mjs    # prebuild: stages blocks/*/output/... into public/blocks/
    stage-models.mjs    # prebuild: stages checksummed sky130 SPICE model decks (issue #149)
    sky130-models.NOTICE.txt  # Apache-2.0 attribution staged alongside model decks
    playground-perf.mjs  # perf harness: reproduces the spike's engine timing (issue #150)
    prerender.mjs        # build: client + SSR Vite builds -> static dist/<route>/index.html
    html-template.mjs    # shared HTML injection helper (prerender.mjs + vite.config.ts's dev SSR)
  public/
    placeholder-layout.svg  # thumbnail fallback for no_artifacts/no-renders blocks
  src/
    entry-client.tsx     # browser hydration entry
    entry-server.tsx     # Node-only render entry (SSR build + dev middleware)
    App.tsx               # picks IndexPage vs DetailPage from injected route data
    index.css             # Tailwind import + dark-theme @theme tokens
    components/
      Header.tsx          # site header — GitHub repo link
      Footer.tsx           # site footer — build SHA, copyright, scope link
      LayoutCard.tsx        # one gallery card per block
      ui/
        badge.tsx           # shadcn/ui-style Badge primitive
        card.tsx             # shadcn/ui-style Card primitives
        table.tsx            # shadcn/ui-style Table primitives
      waveform/
        WaveformViewer.tsx    # interactive Signals-section viewer (#100)
        WaveformViewer.test.tsx
        waveformMath.ts        # pure zoom/cursor/format helpers
        waveformMath.test.ts
        types.ts                # WaveformData artifact shape (mirrors klt sim)
    data/
      types.ts             # Layout type (schema v1, mirrors klt layout-metrics)
      loadLayouts.ts        # build-time layout data loader
      loadLayouts.test.ts
      buildInfo.ts          # build-time git SHA lookup for the footer
      buildInfo.test.ts
    lib/
      utils.ts             # shadcn's `cn` class-merging helper
      blockAssets.ts        # block-relative asset URL helper (renders/signals)
      playground/           # client-side SPICE playground engine wrapper (#150)
        netlist.ts          # pure stimulus-params -> netlist assembly
        netlist.test.ts
        engine.ts            # lazy-load / cache / run eecircuit-engine (WASM ngspice)
        engine.test.ts
        index.ts             # public re-exports
    pages/
      IndexPage.tsx         # landing page (closed-loop vision statement) + gallery card grid
      DetailPage.tsx         # per-block detail page
    setupTests.ts           # vitest global setup (@testing-library/jest-dom matchers)
```

### Scope

Issue #59 shipped the scaffold, the layout data loader, and a landing page
that folds in #11's closed-loop vision statement plus a placeholder block
list proving the loader end-to-end. The gallery index (#63) replaced that
placeholder list with the polished card grid described above. The per-block
detail page (#64) added renders, full metrics, name/description, and a
downloads section gated on #62. The deploy pipeline itself
(`scripts/deploy-site.sh` building this project and publishing `site/dist/`)
is #65. Issue #92 (Epic #90 Phase 1) replatformed all of the above from
Astro to Vite + React + Tailwind + shadcn/ui without changing URLs, content,
the data contract, or the deploy story — laying the groundwork for Epic
#90's interactive phases. Issue #100 (Epic #90 Phase 2) added the Signals
section and its waveform viewer, built against a hand-written fixture (see
`blocks/README.md`'s "Signals fixture" note) since the signals pipeline
issue (#99) that will produce this data for real had not yet merged.
