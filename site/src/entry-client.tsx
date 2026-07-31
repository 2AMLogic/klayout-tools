import { hydrateRoot } from "react-dom/client";
import { App, type PageData } from "./App";
import "./index.css";

/**
 * Browser hydration entry (issue #92 migration from Astro).
 *
 * Both `npm run dev` (via the SSR dev middleware in `vite.config.ts`) and
 * `npm run build` (via `scripts/prerender.mjs`) inject the same `PageData`
 * used to server-render the page as `window.__KLT_DATA__`, so hydration
 * produces markup identical to what was already sent over the wire. There
 * is no client-side router: navigation between `/` and `/<slug>/` is a
 * normal full-page load, matching the pre-migration static site.
 */
declare global {
  interface Window {
    __KLT_DATA__?: PageData;
  }
}

const container = document.getElementById("root");
const data = window.__KLT_DATA__;

if (!container) {
  throw new Error("entry-client: #root element not found");
}
if (!data) {
  throw new Error("entry-client: window.__KLT_DATA__ was not injected by the server/prerender step");
}

hydrateRoot(container, <App data={data} />);
