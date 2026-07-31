import type { Layout } from "@/data/types";
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
 */
export type PageData =
  | { page: "index"; layouts: Layout[]; footer: FooterProps }
  | { page: "detail"; layout: Layout };

export function App({ data }: { data: PageData }) {
  if (data.page === "detail") {
    return <DetailPage layout={data.layout} />;
  }

  return (
    <>
      <Header />
      <IndexPage layouts={data.layouts} />
      <Footer {...data.footer} />
    </>
  );
}
