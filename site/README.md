# klayout-tools.org site

This directory holds the Astro project that builds and deploys
klayout-tools.org (Epic #13). It also still carries
[`index.html`](index.html), the pre-gallery static landing page — retained
for reference but no longer part of the deploy target as of #65; it is
superseded by the Astro `src/pages/index.astro` page built into
`site/dist/`.

## Build and deploy

[`../scripts/deploy-site.sh`](../scripts/deploy-site.sh) builds this Astro
project (`npm --prefix site ci && npm --prefix site run build`, producing
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

## Astro gallery project

Self-contained Astro project: it has its own `package.json` and does **not**
depend on the repository's Python tooling or root `package.json`.

### Quick start

```bash
cd site
npm install      # installs Astro + TypeScript into site/node_modules
npm run dev      # serves the site at http://localhost:4321/
npm run build    # produces a static site in site/dist/
npm run preview  # serves the built site/dist/ locally
npm run check    # Astro + TypeScript type check
npm test         # runs the loader unit tests (vitest)
```

`npm run build` succeeds even on a fresh checkout with **zero**
`layout.json` files present — every block without data is listed as
`status: no_artifacts`.

### Layout data

The build-time loader (`src/data/loadLayouts.ts`) discovers block
directories under the repository's `blocks/` tree and reads each block's
`blocks/<slug>/output/layout.json`.

The loader is resilient to missing data:

- **No `layout.json`** → a stub record with `status: "no_artifacts"`.
- **Unknown `schema_version`** → skipped with a warning (a stub is emitted),
  so the build still completes with the remaining blocks.
- **Valid `layout.json`** → parsed into a typed `Layout` (see
  `src/data/types.ts`).

Blocks are discovered as immediate subdirectories of `blocks/`, skipping
hidden / `_`-prefixed entries.

### Block detail pages

`src/pages/[slug].astro` generates a static `/<slug>` route for every block
`loadLayouts()` discovers (issue #64), showing its renders, metrics,
name/description, and (once #62's public-repo gate sets `downloadable:
true`) a download link — `no_artifacts` blocks render a clear "not built"
state instead of a broken page.

Render images (and, once downloadable, the source layout file) live outside
`site/` under `blocks/<slug>/output/...`; Astro's static build cannot
reference files outside its project root, so the `predev`/`prebuild` npm
hooks run `scripts/copy-renders.mjs` first, which stages them into
`site/public/blocks/<slug>/...` (git-ignored, regenerated every run) so they
are served from `/blocks/<slug>/...`.

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

### Layout

```
site/
  README.md          # this file
  index.html          # pre-gallery static landing page (kept for reference,
                       # no longer deployed as of #65)
  package.json        # Astro + TypeScript dependencies (isolated from repo root)
  astro.config.mjs    # Astro configuration (static output)
  tsconfig.json       # extends astro/tsconfigs/strict
  dist/                # build output (git-ignored, deploy target as of #65)
  scripts/
    copy-renders.mjs  # prebuild: stages blocks/*/output/... into public/blocks/
  src/
    data/
      types.ts         # Layout type (schema v1, mirrors klt layout-metrics / #61)
      loadLayouts.ts    # build-time layout data loader
      loadLayouts.test.ts
    pages/
      index.astro       # landing page (folds in #11) + placeholder block list
      [slug].astro       # per-block detail page (#64)
```

### Scope

Issue #59 shipped the scaffold, the layout data loader, and a landing page
that folds in #11's closed-loop vision statement plus a placeholder block
list proving the loader end-to-end. The polished gallery index (cards,
renders, metrics) is #63; the per-block detail page (#64) is done — renders,
metrics, name/description, and a downloads section gated on #62. The deploy
pipeline itself (`scripts/deploy-site.sh` building this project and
publishing `site/dist/`) is #65, also done.
