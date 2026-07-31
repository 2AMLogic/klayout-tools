// @ts-check
import { defineConfig } from "astro/config";

// klayout-tools.org gallery — static site.
// The layout data loader (src/data/loadLayouts.ts) runs at build time and
// reads layout.json files from the sibling ../blocks directory. See
// site/README.md.
export default defineConfig({
  site: "https://klayout-tools.org",
  // Static output: every page is pre-rendered to HTML at build time.
  output: "static",
});
