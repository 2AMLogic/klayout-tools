/**
 * Build-time static prerender pass (issue #92: Astro -> Vite/React
 * migration).
 *
 * `npm run build` runs `tsc -b` (type-check) then this script, which:
 *   1. Runs a normal Vite client build (`dist/`) — the hydratable React
 *      bundle, hashed assets, and a generic `dist/index.html` app shell.
 *   2. Runs a Vite SSR build of `src/entry-server.tsx` to a throwaway
 *      `dist-ssr/` directory, then imports it directly in this Node
 *      process.
 *   3. Enumerates every route the same way the old Astro
 *      `getStaticPaths()` did — `/` plus one `/<slug>/` per block from
 *      `loadLayouts()` — renders each to an HTML string, and injects it
 *      (via `scripts/html-template.mjs`, shared with the dev SSR
 *      middleware in `vite.config.ts`) into a copy of the client-built
 *      `dist/index.html`, writing `dist/index.html` and
 *      `dist/<slug>/index.html`.
 *   4. Removes `dist-ssr/` — it is a build-time-only intermediate, never
 *      deployed.
 *
 * The result is a fully static `dist/` tree (same directory + index.html
 * per-route shape Astro produced) with no server runtime required to serve
 * it — `scripts/deploy-site.sh` publishes it to Cloudflare Pages unchanged.
 */

import { build } from "vite";
import { readFileSync, writeFileSync, mkdirSync, rmSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { injectPage } from "./html-template.mjs";

const SCRIPT_DIR = dirname(fileURLToPath(import.meta.url));
const SITE_DIR = resolve(SCRIPT_DIR, "..");
const DIST_DIR = join(SITE_DIR, "dist");
const SSR_OUT_DIR = join(SITE_DIR, "dist-ssr");

async function main() {
  console.log("[prerender] building client bundle...");
  await build({ root: SITE_DIR, logLevel: "warn" });

  console.log("[prerender] building SSR bundle...");
  await build({
    root: SITE_DIR,
    logLevel: "warn",
    build: {
      ssr: "src/entry-server.tsx",
      outDir: "dist-ssr",
      emptyOutDir: true,
      copyPublicDir: false,
    },
  });

  const ssrEntry = join(SSR_OUT_DIR, "entry-server.js");
  const mod = await import(pathToFileURL(ssrEntry).href);
  const { getLayouts, getEmExport, getFooterProps, renderPage, INDEX_META, detailMeta } = mod;

  const layouts = getLayouts();
  const template = readFileSync(join(DIST_DIR, "index.html"), "utf-8");

  // Index page: overwrite dist/index.html in place.
  const indexData = { page: "index", layouts, footer: getFooterProps() };
  const indexHtml = injectPage(template, {
    appHtml: renderPage(indexData),
    ...INDEX_META,
    data: indexData,
  });
  writeFileSync(join(DIST_DIR, "index.html"), indexHtml);

  // One /<slug>/index.html per block, matching the old Astro
  // getStaticPaths() route shape exactly.
  for (const layout of layouts) {
    const detailData = { page: "detail", layout, emExport: getEmExport(layout.slug) };
    const html = injectPage(template, {
      appHtml: renderPage(detailData),
      ...detailMeta(layout),
      data: detailData,
    });
    const outDir = join(DIST_DIR, layout.slug);
    mkdirSync(outDir, { recursive: true });
    writeFileSync(join(outDir, "index.html"), html);
  }

  rmSync(SSR_OUT_DIR, { recursive: true, force: true });

  console.log(`[prerender] wrote ${1 + layouts.length} page(s) to ${DIST_DIR}`);
}

main().catch((err) => {
  console.error(err);
  process.exitCode = 1;
});
