import { useState } from "react";
import { PatchAntennaResult } from "./PatchAntennaResult";
import { ProvenancePanel } from "./ProvenancePanel";
import type { EmProvenance } from "./types";

/**
 * One "Electromagnetics" gallery entry (Epic #840 Phase 1c, issue #851) for
 * the patch-antenna benchmark: title + description, `PatchAntennaResult`
 * (issue #850's mesh/field + S11 viewer), and a `ProvenancePanel` sourced
 * from the *same* fetched export document via `PatchAntennaResult`'s
 * `onLoad` callback — no second fetch of `dataUrl` just to reach
 * `data.provenance`.
 *
 * A future benchmark (spiral inductor L/Q, Mie sphere — see
 * `docs/design/em-field-sim-spike.md`) gets its own `<Benchmark>Result` +
 * `<Benchmark>GalleryEntry` pair the same shape as this one, added to
 * `EmGallerySection`'s benchmark list once a real, committed geode-fem
 * export exists for it (never a placeholder/illustrative one — see
 * `EmGallerySection`'s module doc).
 */
export function PatchAntennaGalleryEntry() {
  const [provenance, setProvenance] = useState<EmProvenance | null>(null);

  return (
    <article
      className="flex flex-col gap-4 rounded-lg border border-border bg-panel px-5 py-4"
      data-testid="em-benchmark-entry-patch-antenna"
    >
      <header>
        <h3 className="font-mono text-[1.05rem] text-orange">Patch antenna — S11 + near field</h3>
        <p className="mt-1 text-[0.85rem] leading-[1.6] text-fog-dim">
          Probe-fed FR-4 microstrip patch, driven full-wave FEM solve swept 2.0–3.0 GHz.
          Validated against a Balanis cavity-model oracle for resonant frequency and
          broadside directivity.
        </p>
      </header>

      <PatchAntennaResult onLoad={(data) => setProvenance(data.provenance)} />

      {provenance && <ProvenancePanel provenance={provenance} />}
    </article>
  );
}
