import { useState } from "react";
import type { Layout } from "@/data/types";
import {
  filterLayouts,
  isCanary,
  pdksIn,
  statusGroup,
  type PdkFilter,
  type StatusFilter,
} from "@/lib/gallery";
import { LayoutCard } from "@/components/LayoutCard";
import { EmGallerySection } from "@/components/em";

/**
 * klayout-tools.org landing page (ported from `index.astro` in issue #92's
 * Astro -> React migration; originally issue #59/#63).
 *
 * Folds in the closed-loop vision statement + roadmap/architecture links
 * (issue #11, mirroring `site/index.html`) and renders the gallery index —
 * one card per block discovered at build time by `loadLayouts()` (issue
 * #63). Each card links to its `/<slug>/` detail page (issue #64).
 *
 * Below the layout gallery, `EmGallerySection` (Epic #840 Phase 1c, issue
 * #851; demoted to a compact "Solver Validation" strip in Phase 3c, issue
 * #960) renders geode-fem solver-validation results — mesh/field overlay +
 * S-parameter/capacitance results, each with a provenance panel — as
 * evidence that the solver behind each project's Field Data panel
 * (`DetailPage.tsx`, Phase 3b/#959) matches known answers. It is
 * independent of `layouts` — its data comes from the staged
 * `examples/em/*.em-export.json` exports, not the block-metrics pipeline —
 * and is visually subordinate to the project gallery above it (h3 heading,
 * compact spacing, muted styling).
 */
export interface IndexPageProps {
  layouts: Layout[];
}

export function IndexPage({ layouts }: IndexPageProps) {
  const [pdk, setPdk] = useState<PdkFilter>("all");
  const [status, setStatus] = useState<StatusFilter>("all");
  // Canary blocks lead the gallery; the standard-cell test corpus is a footnote.
  const canaries = layouts.filter(isCanary);
  const corpus = layouts.filter((l) => !isCanary(l));
  const withData = canaries.filter((l) => l.status !== "no_artifacts").length;
  const shown = filterLayouts(canaries, pdk, status);
  const pdks = pdksIn(canaries);
  const statuses = (
    [
      ["layout", "Layout"],
      ["partial", "Partial"],
      ["in-design", "In design"],
      ["no-artifacts", "No artifacts"],
    ] as const
  ).filter(([key]) => canaries.some((l) => statusGroup(l) === key));
  const chip = (active: boolean) =>
    `rounded-full border px-[0.7rem] py-[0.1rem] text-[0.8rem] ${
      active ? "border-orange text-orange" : "border-border text-fog-dim"
    }`;

  return (
    <main className="mx-auto max-w-[72rem] flex-1 px-6 py-12">
      <div className="max-w-[44rem]">
        <h1 className="font-mono text-[2.2rem] text-orange [text-shadow:0_0_6px_rgba(255,160,40,0.9),0_0_24px_rgba(255,160,40,0.4)]">
          klayout-tools
        </h1>
        <p className="mt-2 text-[0.85rem] tracking-[0.25em] text-cyan uppercase [text-shadow:0_0_6px_rgba(62,224,232,0.8),0_0_20px_rgba(62,224,232,0.3)]">
          IC layout for AI agents
        </p>
        <span className="mt-[1.2rem] inline-block rounded-full border border-orange px-[0.7rem] py-[0.15rem] text-[0.8rem] text-orange">
          scaffold — in progress, in the open
        </span>

        <p className="mt-8 mb-[1.1rem] leading-[1.7]">
          Standalone tools that let AI agents parse, check, and edit chip layouts:
          GDSII and OASIS streams, DRC decks, headless everything, machine-readable
          JSON everywhere. Built on{" "}
          <a href="https://www.klayout.de">KLayout</a>'s Python API.
        </p>
        <p className="my-[1.1rem] leading-[1.7]">
          It is the <a href="https://kicad-tools.org">kicad-tools</a> playbook one
          layer down the stack. Over there, agents take a design from schematic to
          routed PCB to fabrication package, and a gallery of real boards proves it.
          Here, the same pattern meets silicon.
        </p>
        <p className="my-[1.1rem] leading-[1.7]">
          An agent can take a spec → schematic/generator → sized circuit → layout
          → DRC/LVS clean → extracted netlist → simulation-verified, on an open
          PDK, unaided — with every step headless and JSON-contracted. That
          closed loop is the target.
        </p>

        <pre className="my-6 overflow-x-auto rounded-lg border border-border bg-panel px-5 py-4 font-mono text-[0.85rem] leading-[1.7] text-fog">
          <span className="text-fog-dim"># the interface contract we are building toward</span>
          {"\n"}klt layers design.gds              <span className="text-fog-dim"># enumerate layers, JSON out</span>
          {"\n"}klt cells design.gds --top         <span className="text-fog-dim"># cell hierarchy</span>
          {"\n"}klt drc design.gds --deck sky130   <span className="text-fog-dim"># DRC, structured violations</span>
          {"\n"}klt stats design.gds --per-layer   <span className="text-fog-dim"># densities, bbox, counts</span>
        </pre>

        <p className="my-[1.1rem] leading-[1.7] text-fog-dim">
          Progress lands in the repo first; worked examples appear here the
          way boards appear in the kicad-tools gallery. See the{" "}
          <a href="https://github.com/2AMLogic/klayout-tools/blob/main/ROADMAP.md">
            roadmap
          </a>{" "}
          for the build order and the{" "}
          <a href="https://github.com/2AMLogic/klayout-tools/blob/main/docs/ARCHITECTURE.md">
            architecture
          </a>{" "}
          doc for how the pieces fit.
        </p>
      </div>

      <h2 className="mt-10 font-mono text-[1.3rem] text-cyan">Gallery</h2>
      <p className="my-[1.1rem] max-w-[44rem] leading-[1.7] text-fog-dim">
        Blocks discovered by the build-time layout data loader (
        <code>src/data/loadLayouts.ts</code>), each rendered from its{" "}
        <code>klt layout-metrics</code> output.
      </p>
      <p className="text-[0.85rem] text-fog-dim">
        {canaries.length} canary block{canaries.length === 1 ? "" : "s"}
        {withData > 0 && ` · ${withData} with generated artifacts`}
        {shown.length !== canaries.length && ` · showing ${shown.length}`}
      </p>

      {canaries.length > 0 && (
        <div className="mt-3 flex flex-col gap-2" aria-label="Gallery filters">
          <div role="group" aria-label="Filter by PDK" className="flex flex-wrap gap-2">
            {(["all", ...pdks] as const).map((p) => (
              <button
                key={p}
                type="button"
                aria-pressed={pdk === p}
                className={chip(pdk === p)}
                onClick={() => setPdk(p)}
              >
                {p === "all" ? "All PDKs" : p}
              </button>
            ))}
          </div>
          <div role="group" aria-label="Filter by status" className="flex flex-wrap gap-2">
            {([["all", "Any status"], ...statuses] as const).map(([key, label]) => (
              <button
                key={key}
                type="button"
                aria-pressed={status === key}
                className={chip(status === key)}
                onClick={() => setStatus(key)}
              >
                {label}
              </button>
            ))}
          </div>
        </div>
      )}

      {layouts.length === 0 && (
        <p className="my-[1.1rem] max-w-[44rem] leading-[1.7] text-fog-dim">
          No blocks discovered. Add blocks under <code>blocks/</code> and run{" "}
          <code>klt layout-metrics</code> to populate data.
        </p>
      )}

      {canaries.length > 0 && shown.length === 0 && (
        <p className="my-[1.1rem] text-fog-dim">No blocks match these filters.</p>
      )}

      <ul role="list" className="mt-5 grid list-none grid-cols-[repeat(auto-fill,minmax(260px,1fr))] gap-5 p-0 max-[480px]:grid-cols-1">
        {shown.map((layout) => (
          <li key={layout.slug} className="flex">
            <div className="w-full">
              <LayoutCard layout={layout} />
            </div>
          </li>
        ))}
      </ul>

      {corpus.length > 0 && (
        <section aria-label="Test corpus" className="mt-10">
          <h3 className="font-mono text-[1rem] text-fog-dim">Test corpus</h3>
          <p className="my-2 max-w-[44rem] text-[0.85rem] leading-[1.7] text-fog-dim">
            Standard cells from the <code>tests/corpus</code> demo set — the
            loader's test fixtures, not 2AM designs.
          </p>
          <ul role="list" className="flex list-none flex-wrap gap-x-4 gap-y-1 p-0 text-[0.85rem]">
            {corpus.map((l) => (
              <li key={l.slug}>
                <a href={`/${l.slug}/`}>{l.name}</a>
              </li>
            ))}
          </ul>
        </section>
      )}

      <EmGallerySection />
    </main>
  );
}
