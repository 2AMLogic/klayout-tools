# klayout-tools.org site

This directory holds two things:

- [`index.html`](index.html) — the single static landing page currently
  **deployed** to Cloudflare Pages (project `klayout-tools`, custom domain
  klayout-tools.org) via [`../scripts/deploy-site.sh`](../scripts/deploy-site.sh).
- An Astro project (this README's focus below) — the future gallery site
  (Epic #13), not yet wired into the deploy pipeline (#65). It coexists with
  `index.html` until the deploy pipeline switches over and `index.html` is
  retired.

To publish a change to the *currently live* page, edit `index.html`, then run
the deploy script (see [`scripts/README.md`](../scripts/README.md) for the
auth flow):

```
source ~/.cloudflare/rjwalters/pages-rjwalters.env
scripts/deploy-site.sh
```

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
  index.html          # the currently-deployed static landing page
  package.json        # Astro + TypeScript dependencies (isolated from repo root)
  astro.config.mjs    # Astro configuration (static output)
  tsconfig.json       # extends astro/tsconfigs/strict
  src/
    data/
      types.ts         # Layout type (schema v1, mirrors klt layout-metrics / #61)
      loadLayouts.ts    # build-time layout data loader
      loadLayouts.test.ts
    pages/
      index.astro       # landing page (folds in #11) + placeholder block list
```

### Scope

This issue (#59) ships the scaffold, the layout data loader, and a landing
page that folds in #11's closed-loop vision statement plus a placeholder
block list proving the loader end-to-end. The polished gallery index (cards,
renders, metrics) is #63, and the per-block detail page is #64.
