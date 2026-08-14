import { renderToString } from "react-dom/server";
import { join } from "node:path";
import { App, type PageData } from "./App";
import { blocksDir, loadLayouts } from "./data/loadLayouts";
import { loadEmExport } from "./data/loadEmExport";
import { getBuildSha, commitUrl } from "./data/buildInfo";
import type { Layout } from "./data/types";
import type { EmSiteExport } from "./components/em/types";
import type { FooterProps } from "./components/Footer";

/**
 * Server-only entry point (issue #92 migration from Astro).
 *
 * Imported two ways, both Node-only / never bundled into the browser build:
 *   - `scripts/prerender.mjs` loads this module via a Vite SSR build to
 *     render every route to static HTML at `npm run build` time.
 *   - `vite.config.ts`'s dev middleware calls `server.ssrLoadModule` on this
 *     same file so `npm run dev` serves real (HMR'd) content instead of a
 *     placeholder.
 *
 * `loadLayouts`/`getBuildSha` touch `node:fs`/`node:child_process` — safe
 * here, but must never be imported from client-bundled code (see
 * `src/components/Footer.tsx`'s doc comment).
 */
const REPO_URL = "https://github.com/2AMLogic/klayout-tools";

export function getLayouts(): Layout[] {
  return loadLayouts();
}

/**
 * Build-time field-data lookup for one block's detail page (Epic #840 Phase
 * 3b, issue #959) — `blocks/<slug>/output/<slug>.em-export.json`, when
 * present and parsable, else `null` (no panel). See `data/loadEmExport.ts`
 * for the full resilience contract (missing/malformed/unknown-version ->
 * `null`, never throws).
 */
export function getEmExport(slug: string): EmSiteExport | null {
  return loadEmExport(join(blocksDir(), slug));
}

export function getFooterProps(): FooterProps {
  const build = getBuildSha();
  return {
    repoUrl: REPO_URL,
    buildSha: build.sha,
    buildUrl: commitUrl(REPO_URL, build),
    year: new Date().getFullYear(),
  };
}

export function renderPage(data: PageData): string {
  return renderToString(<App data={data} />);
}

export const INDEX_META = {
  title: "klayout-tools — IC layout for AI agents",
  description:
    "Standalone tools that let AI agents parse, check, and edit chip layouts. The kicad-tools playbook, one layer down the stack. In progress, in the open.",
};

export function detailMeta(layout: Layout): { title: string; description?: string } {
  return {
    title: `${layout.name} — klayout-tools`,
    description: layout.description,
  };
}
