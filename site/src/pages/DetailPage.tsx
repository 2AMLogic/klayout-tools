import type { Layout } from "@/data/types";
import { Table, TableBody, TableRow, TableHead, TableCell } from "@/components/ui/table";
import { WaveformViewer } from "@/components/waveform";
import { StimulusPlayground, isPlaygroundEligible } from "@/components/playground";
import { blockAssetUrl } from "@/lib/blockAssets";

/**
 * Per-block detail page (ported from `[slug].astro` in issue #92's Astro ->
 * React migration; originally issue #64, Epic #13 gallery).
 *
 * One static route `/<slug>/` per block discovered by `loadLayouts()`
 * (including `no_artifacts` stubs, so the gallery index never links to a
 * 404). Shows the block's available per-layer renders (the `renders` map,
 * staged by `site/scripts/copy-renders.mjs`), name + description, source
 * provenance (`layout.source`, issue #62), a Specification section gated
 * behind `layout.spec_summary` (issue #62, canary blocks' target-spec
 * table), a metrics table covering every present optional field
 * (omit-absent rule — a missing field is never rendered as
 * "null"/"undefined"), a Signals section (issue #100, Epic #90 Phase 2)
 * gated behind `layout.signals` and rendered by `@/components/waveform`'s
 * `WaveformViewer` — or, for a sky130 gallery cell with a staged playground
 * model deck (Epic #90 phase C, issue #151), `@/components/playground`'s
 * `StimulusPlayground`, which wraps the same viewer with editable stimulus
 * controls and a client-side ngspice re-run — a downloads section gated
 * behind `layout.downloadable`, and a back-link to the gallery index.
 *
 * The downloads section also links out to Tiny Tapeout's hosted GDS/OAS
 * viewer (issue #249, Option 1 of that issue: link-out to a hosted viewer
 * rather than embedding one in this site — cheapest way to prove the
 * pattern, revisit only if the operator wants a same-origin viewer later)
 * so a reviewer can zoom/pan/toggle layers without a local KLayout install.
 * Every gallery block is currently sky130, so the PDK query param is
 * hardcoded (`sky130A`, Tiny Tapeout's own PDK identifier) rather than
 * plumbed through a new `Layout` field — revisit if a non-sky130 PDK is
 * ever added to the gallery.
 *
 * Matches the original Astro page's chrome exactly: no shared Header/Footer
 * — this page has always been a standalone document (see `[slug].astro`,
 * which never imported `Layout.astro`), preserved here rather than "fixed"
 * as part of a like-for-like platform migration.
 */
export interface DetailPageProps {
  layout: Layout;
}

const STATUS_BORDER_CLASS: Record<Layout["status"], string> = {
  ok: "border-cyan text-cyan",
  partial: "border-orange text-orange",
  no_artifacts: "border-fog-dim text-fog-dim",
  "in design — simulation evidence": "border-cyan text-cyan",
};

/**
 * Deployed origin for the static site (see `CLAUDE.md`: klayout-tools.org).
 * Hardcoded rather than derived from `window.location` because this page is
 * also rendered server-side at build time (`entry-server.tsx` via
 * `scripts/prerender.mjs`), where no `window` exists — and the viewer link
 * needs an absolute, publicly fetchable URL regardless of render context
 * (Tiny Tapeout's hosted viewer fetches the file cross-origin).
 */
const SITE_ORIGIN = "https://klayout-tools.org";

/** Every gallery block is currently sky130 (see module doc comment above). */
const GALLERY_PDK = "sky130A";

/**
 * Builds a Tiny Tapeout hosted-viewer link for a block's layout file (issue
 * #249). Mirrors Tiny Tapeout's own `?model=<url>&pdk=<pdk>` convention
 * (github.com/TinyTapeout/tt-gds-action) so the file can be inspected
 * (zoom/pan/layer toggle) from a browser with no local KLayout install.
 */
function gdsViewerUrl(fileUrl: string): string {
  const params = new URLSearchParams({ model: fileUrl, pdk: GALLERY_PDK });
  return `https://gds-viewer.tinytapeout.com/?${params.toString()}`;
}

export function DetailPage({ layout }: DetailPageProps) {
  const displayName = layout.name;
  const isPreLayout = layout.status === "in design — simulation evidence";
  const isBuilt = layout.status !== "no_artifacts" && !isPreLayout;

  const renderEntries = Object.entries(layout.renders ?? {}).sort(([a], [b]) => a.localeCompare(b));

  type Row = { label: string; value: string };
  const rows: Row[] = [];
  if (layout.layer_count !== undefined) {
    rows.push({ label: "Layers", value: `${layout.layer_count}` });
  }
  if (layout.cell_count !== undefined) {
    rows.push({ label: "Cells", value: `${layout.cell_count}` });
  }
  if (layout.instance_count !== undefined) {
    rows.push({ label: "Instances", value: `${layout.instance_count}` });
  }
  if (layout.drc?.deck !== undefined) {
    rows.push({ label: "DRC Deck", value: layout.drc.deck });
  }
  if (layout.drc?.status !== undefined) {
    rows.push({ label: "DRC Status", value: layout.drc.status });
  }
  if (layout.drc?.violation_count !== undefined) {
    rows.push({ label: "DRC Violations", value: `${layout.drc.violation_count}` });
  }

  const canDownload = layout.downloadable === true && layout.layout_file !== undefined;
  const layoutFileUrl = canDownload
    ? `${SITE_ORIGIN}${blockAssetUrl(layout.slug, layout.layout_file as string)}`
    : undefined;

  return (
    <main className="mx-auto max-w-[44rem] px-6 pt-12 pb-16">
      <p className="text-[0.9rem]">
        <a href="/">&larr; Back to gallery</a>
      </p>

      <header className="mt-6">
        <h1 className="inline font-mono text-[1.8rem] break-words text-orange">{displayName}</h1>
        <span
          className={`ml-[0.6rem] inline-block rounded-full border px-[0.7rem] py-[0.15rem] align-middle text-[0.75rem] tracking-[0.04em] uppercase ${STATUS_BORDER_CLASS[layout.status]}`}
        >
          {layout.status}
        </span>
        {layout.description && <p className="mt-3 leading-[1.6] text-fog">{layout.description}</p>}
        {layout.slug !== displayName && (
          <p className="mt-1.5 font-mono text-[0.8rem] text-fog-dim">{layout.slug}</p>
        )}
        {layout.source && (
          <p className="mt-1.5 font-mono text-[0.8rem] text-fog-dim">
            Source:{" "}
            <a href={`https://github.com/${layout.source.repo}`}>{layout.source.repo}</a>
            {" @ "}
            {layout.source.ref.slice(0, 12)}
          </p>
        )}
      </header>

      {isPreLayout && (
        <p className="mt-6 rounded-lg border border-l-[3px] border-border border-l-cyan bg-panel px-[1.1rem] py-[0.9rem] text-fog-dim">
          This block is still in design — no layout exists yet. The specification and
          signals below are simulation evidence from the source repo, not
          measured/extracted silicon.
        </p>
      )}
      {!isBuilt && !isPreLayout && (
        <p className="mt-6 rounded-lg border border-l-[3px] border-border border-l-fog-dim bg-panel px-[1.1rem] py-[0.9rem] text-fog-dim">
          This block has not been built yet — no renders, metrics, or downloads are
          available.
        </p>
      )}

      {layout.spec_summary && (
        <section aria-label="Specification" className="mt-9">
          <h2 className="mb-4 font-mono text-[1.1rem] text-cyan">Specification</h2>
          {layout.spec_summary.status_note && (
            <p className="mb-3 text-[0.85rem] text-fog-dim">{layout.spec_summary.status_note}</p>
          )}
          <Table>
            <TableBody>
              {layout.spec_summary.rows.map((row) => (
                <TableRow key={row.parameter}>
                  <TableHead>{row.parameter}</TableHead>
                  <TableCell>
                    {row.target}
                    {row.stretch && (
                      <span className="text-fog-dim"> · stretch: {row.stretch}</span>
                    )}
                    {row.corner_binding && (
                      <span className="block text-[0.8rem] text-fog-dim">
                        {row.corner_binding}
                      </span>
                    )}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </section>
      )}

      <section aria-label="Renders" className="mt-9">
        <h2 className="mb-4 font-mono text-[1.1rem] text-cyan">Renders</h2>
        {renderEntries.length > 0 ? (
          <div className="grid grid-cols-[repeat(auto-fill,minmax(9rem,1fr))] gap-4">
            {renderEntries.map(([id, relPath]) => (
              <figure key={id} className="overflow-hidden rounded-lg border border-border bg-panel">
                <img
                  src={blockAssetUrl(layout.slug, relPath)}
                  alt={`Render "${id}" of ${displayName}`}
                  loading="lazy"
                  decoding="async"
                  className="block aspect-4/3 w-full bg-night object-contain"
                />
                <figcaption className="border-t border-border px-2.5 py-1.5 text-center font-mono text-[0.75rem] text-fog-dim">
                  {id}
                </figcaption>
              </figure>
            ))}
          </div>
        ) : (
          <p className="text-fog-dim">No renders yet.</p>
        )}
      </section>

      <section aria-label="Metrics" className="mt-9">
        <h2 className="mb-4 font-mono text-[1.1rem] text-cyan">Metrics</h2>
        {rows.length > 0 ? (
          <Table>
            <TableBody>
              {rows.map((row) => (
                <TableRow key={row.label}>
                  <TableHead>{row.label}</TableHead>
                  <TableCell>{row.value}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        ) : (
          <p className="text-fog-dim">No metrics available.</p>
        )}
      </section>

      {layout.signals !== undefined && (
        <section aria-label="Signals" className="mt-9">
          <h2 className="mb-4 font-mono text-[1.1rem] text-cyan">Signals</h2>
          {isPlaygroundEligible(layout.slug) ? (
            <StimulusPlayground slug={layout.slug} signals={layout.signals} />
          ) : (
            <WaveformViewer slug={layout.slug} signals={layout.signals} />
          )}
        </section>
      )}

      <section aria-label="Downloads" className="mt-9">
        <h2 className="mb-4 font-mono text-[1.1rem] text-cyan">Downloads</h2>
        {canDownload && layoutFileUrl ? (
          <ul className="flex list-none flex-col gap-2.5 p-0">
            <li>
              <a
                href={blockAssetUrl(layout.slug, layout.layout_file as string)}
                download
                className="inline-block rounded-lg border border-border bg-panel px-[0.9rem] py-[0.55rem] text-[0.9rem] text-fog no-underline hover:border-cyan focus-visible:border-cyan focus-visible:outline-none"
              >
                Layout ({layout.layout_file})
              </a>
            </li>
            <li>
              <a
                href={gdsViewerUrl(layoutFileUrl)}
                target="_blank"
                rel="noopener noreferrer"
                className="inline-block rounded-lg border border-border bg-panel px-[0.9rem] py-[0.55rem] text-[0.9rem] text-fog no-underline hover:border-cyan focus-visible:border-cyan focus-visible:outline-none"
              >
                View in browser
              </a>
            </li>
          </ul>
        ) : (
          <p className="text-fog-dim">No download available.</p>
        )}
      </section>

      <footer className="mt-12 border-t border-border pt-6 text-[0.9rem]">
        <a href="/">&larr; Back to gallery</a>
      </footer>
    </main>
  );
}
