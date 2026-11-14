/// <reference types="vitest/config" />
import { readFileSync } from "node:fs";
import { fileURLToPath, URL } from "node:url";
import { defineConfig, type Plugin } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { injectPage } from "./scripts/html-template.mjs";

const SITE_DIR = fileURLToPath(new URL(".", import.meta.url));

/**
 * Dev-only SSR middleware: serves `/` and `/<slug>/` with real,
 * server-rendered gallery content (instead of an empty shell) so `npm run
 * dev` stays a useful live preview after the Astro -> Vite/React migration
 * (issue #92). Mirrors what `scripts/prerender.mjs` does for `npm run
 * build`, reusing the same `src/entry-server.tsx` render functions and
 * `scripts/html-template.mjs` injection helper so dev and prod output stay
 * behaviorally identical.
 */
function devSsrPlugin(): Plugin {
  return {
    name: "klt-dev-ssr",
    apply: "serve",
    configureServer(server) {
      server.middlewares.use(async (req, res, next) => {
        const url = req.url ?? "/";
        // Only handle actual page navigations — let Vite's own middleware
        // serve everything else (assets, HMR client, /@vite/*, etc.).
        if (req.method !== "GET" || /\.[a-zA-Z0-9]+$/.test(url.split("?")[0] ?? "")) {
          return next();
        }

        try {
          const mod = await server.ssrLoadModule("/src/entry-server.tsx");
          const layouts = mod.getLayouts();

          const slug = url.replace(/^\/+|\/+$/g, "");
          let data;
          let meta: { title: string; description?: string };
          if (slug === "") {
            data = { page: "index", layouts, footer: mod.getFooterProps() };
            meta = mod.INDEX_META;
          } else {
            const layout = layouts.find((l: { slug: string }) => l.slug === slug);
            if (!layout) return next();
            data = { page: "detail", layout };
            meta = mod.detailMeta(layout);
          }

          const appHtml = mod.renderPage(data);
          let template = readFileSync(`${SITE_DIR}index.html`, "utf-8");
          template = await server.transformIndexHtml(url, template);
          const html = injectPage(template, { appHtml, ...meta, data });

          res.statusCode = 200;
          res.setHeader("Content-Type", "text/html");
          res.end(html);
        } catch (err) {
          server.ssrFixStacktrace(err as Error);
          next(err);
        }
      });
    },
  };
}

// klayout-tools.org gallery — static site (migrated from Astro in #92).
//
// The deployed site is a plain static build with no server runtime: the
// client bundle from this config is combined with server-rendered HTML by
// scripts/prerender.mjs (build time) / the devSsrPlugin above (dev time).
// See site/README.md.
export default defineConfig({
  plugins: [react(), tailwindcss(), devSsrPlugin()],
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  build: {
    outDir: "dist",
  },
  test: {
    // Default environment stays "node" — the pre-existing loader/build-info
    // suites don't touch the DOM. Component tests that need one (e.g.
    // `WaveformViewer.test.tsx`) opt in per-file via a
    // `// @vitest-environment jsdom` pragma at the top of the file.
    setupFiles: ["./src/setupTests.ts"],
  },
});
