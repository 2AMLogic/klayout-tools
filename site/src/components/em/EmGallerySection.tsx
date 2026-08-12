import { PatchAntennaGalleryEntry } from "./PatchAntennaGalleryEntry";
import { SpiralInductorGalleryEntry } from "./SpiralInductorGalleryEntry";

/**
 * "Electromagnetics" gallery section (Epic #840 "real E&M results in the
 * browser", Phase 1c, issue #851) — wires Phase 1a's export format (#849)
 * and Phase 1b's `FieldViewer`/`WaveformViewer` integration (#850) into a
 * dedicated section of the landing page, one entry per validated geode-fem
 * benchmark, each carrying its own `ProvenancePanel`.
 *
 * **Benchmark count note (as of issue #877, 2026-08-12):** the epic phase
 * description names "2-3 benchmarks (patch antenna, spiral inductor L/Q,
 * Mie sphere)". Two real, committed geode-fem exports exist today: the
 * patch antenna (`examples/em/patch_antenna/patch_antenna.em-export.json`,
 * #849) and the spiral inductor L/Q
 * (`examples/em/spiral_inductor/spiral_inductor.em-export.json`, #877),
 * satisfying the gallery's ">=2 validated benchmarks" acceptance criterion.
 * Producing the Mie-sphere benchmark still requires a real solve against a
 * local geode-fem checkout (see `docs/design/em-field-sim-spike.md`) — new
 * solver-data generation, not site-wiring, and remains out of scope here
 * per "no illustrative/faked fields": this section only ever ships real
 * entries. Adding a benchmark here is additive: give it its own
 * `<Benchmark>Result` + `<Benchmark>GalleryEntry` pair (see
 * `PatchAntennaGalleryEntry`'s / `SpiralInductorGalleryEntry`'s doc
 * comments) and list it below.
 */
export function EmGallerySection() {
  return (
    <section aria-label="Electromagnetics" className="mt-14" data-testid="em-gallery-section">
      <h2 className="mb-2 font-mono text-[1.3rem] text-cyan">Electromagnetics</h2>
      <p className="mb-5 max-w-[44rem] leading-[1.7] text-fog-dim">
        Real geode-fem field solves, browsable the same way as the layout gallery
        above. Every mesh, field overlay, and S-parameter curve here traces to a
        committed solver run — see each entry&rsquo;s provenance panel for the exact
        solver version, geometry, and solve parameters behind it.
      </p>

      <div className="flex flex-col gap-6">
        <PatchAntennaGalleryEntry />
        <SpiralInductorGalleryEntry />
      </div>
    </section>
  );
}
