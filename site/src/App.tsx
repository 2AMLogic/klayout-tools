import type { Layout } from "@/data/types";
import type { EmSiteExport } from "@/components/em/types";
import { IndexPage } from "@/pages/IndexPage";
import { DetailPage } from "@/pages/DetailPage";
import { Header } from "@/components/Header";
import { Footer, type FooterProps } from "@/components/Footer";

/**
 * Per-route data shape injected into the prerendered HTML as
 * `window.__KLT_DATA__` (see `scripts/prerender.mjs`) and read back by
 * `entry-client.tsx` to hydrate deterministically.
 *
 * The index route wraps its page in the shared `Header`/`Footer` chrome;
 * the detail route intentionally does not — matching the pre-migration
 * Astro site, where `[slug].astro` never rendered through `Layout.astro`.
 *
 * `emExport` (Epic #840 Phase 3b, issue #959) is `null` for the vast
 * majority of blocks (no committed `<slug>.em-export.json` artifact) —
 * `DetailPage` renders unchanged in that case, matching every block before
 * this issue.
 */
export type PageData =
  | { page: "index"; layouts: Layout[]; footer: FooterProps }
  | { page: "detail"; layout: Layout; emExport: EmSiteExport | null };

export function App({ data }: { data: PageData }) {
  if (data.page === "detail") {
    return <DetailPage layout={data.layout} emExport={data.emExport} />;
  }

  return (
    <>
      <Header />
      <IndexPage layouts={data.layouts} />
      <Footer {...data.footer} />
    </>
  );
}
