import { useState } from "react";
import { SpiralInductorResult } from "./SpiralInductorResult";
import { ProvenancePanel } from "./ProvenancePanel";
import type { EmProvenance } from "./types";

/**
 * Second "Electromagnetics" gallery entry (Epic #840, issue #877) for the
 * spiral-inductor L/Q benchmark: title + description, `SpiralInductorResult`
 * (mesh/field + S11/L/Q viewer), and a `ProvenancePanel` sourced from the
 * *same* fetched export document via `SpiralInductorResult`'s `onLoad`
 * callback — no second fetch of `dataUrl` just to reach `data.provenance`.
 * Mirrors `PatchAntennaGalleryEntry`'s shape exactly (see that component's
 * own doc comment for the pattern this is following).
 */
export function SpiralInductorGalleryEntry() {
  const [provenance, setProvenance] = useState<EmProvenance | null>(null);

  return (
    <article
      className="flex flex-col gap-4 rounded-lg border border-border bg-panel px-5 py-4"
      data-testid="em-benchmark-entry-spiral-inductor"
    >
      <header>
        <h3 className="font-mono text-[1.05rem] text-orange">Spiral inductor — L/Q</h3>
        <p className="mt-1 text-[0.85rem] leading-[1.6] text-fog-dim">
          Port-driven 3.5-turn square spiral inductor, full-wave FEM solve swept
          0.1–40 GHz. Validated against the Mohan analytic formula and MoM PEEC
          oracle comparisons (geode-fem's flagship benchmark).
        </p>
      </header>

      <SpiralInductorResult onLoad={(data) => setProvenance(data.provenance)} />

      {provenance && <ProvenancePanel provenance={provenance} />}
    </article>
  );
}
