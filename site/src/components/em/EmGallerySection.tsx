import { InterconnectCouplingGalleryEntry } from "./InterconnectCouplingGalleryEntry";
import { LiveSolveAvailabilityNotice } from "./LiveSolveAvailabilityNotice";
import { PatchAntennaGalleryEntry } from "./PatchAntennaGalleryEntry";
import { SpiralInductorGalleryEntry } from "./SpiralInductorGalleryEntry";

/**
 * "Solver Validation" strip (Epic #840 "real E&M results in the browser",
 * originally the headline "Electromagnetics" gallery from Phase 1c/#851,
 * demoted to a compact validation strip in Phase 3c/#960) — wires Phase 1a's
 * export format (#849) and Phase 1b's `FieldViewer`/`WaveformViewer`
 * integration (#850) into a subordinate section of the landing page, one
 * entry per validated geode-fem benchmark, each carrying its own
 * `ProvenancePanel`.
 *
 * **Demotion note (Phase 3c, issue #960):** since Phase 3b (#959) shipped a
 * per-project Field Data panel on `DetailPage.tsx`, that panel — not this
 * section — carries the headline "real E&M results" story. This section now
 * presents itself as evidence that the solver behind those panels
 * (geode-fem) matches known answers, not as the site's EM showcase: it is
 * visually subordinate to the project gallery above it (h3 vs. the
 * gallery's h2, muted/compact styling, reduced spacing) and every entry and
 * its provenance panel is unchanged from before the demotion — nothing was
 * removed, only the framing and visual weight. `DetailPage.tsx`'s Field
 * Data section links back here (`#solver-validation`) as the validation
 * trail for the field result it renders.
 *
 * **Benchmark count note (as of issue #877, 2026-08-12):** the epic phase
 * description names "2-3 benchmarks (patch antenna, spiral inductor L/Q,
 * Mie sphere)". Two real, committed geode-fem exports satisfy that today:
 * the patch antenna (`examples/em/patch_antenna/patch_antenna.em-export.json`,
 * #849) and the spiral inductor L/Q
 * (`examples/em/spiral_inductor/spiral_inductor.em-export.json`, #877).
 * Producing the Mie-sphere benchmark still requires a real solve against a
 * local geode-fem checkout (see `docs/design/em-field-sim-spike.md`) — new
 * solver-data generation, not site-wiring, and remains out of scope here
 * per "no illustrative/faked fields": this section only ever ships real
 * entries.
 *
 * **Real fleet geometry (issue #842):** `InterconnectCouplingGalleryEntry`
 * is not one of those textbook benchmarks — it closes the loop between this
 * site and the canary program by extracting the real coupling capacitance
 * of a real gallery block's own `met1` power rails (see that component's
 * doc comment and `examples/em/interconnect_coupling/generate.py`), so the
 * gallery ships with a third real entry without waiting on more textbook
 * benchmarks.
 *
 * Adding a benchmark here is additive: give it its own `<Benchmark>Result` +
 * `<Benchmark>GalleryEntry` pair (see `PatchAntennaGalleryEntry`'s /
 * `SpiralInductorGalleryEntry`'s doc comments) and list it below.
 *
 * **Live-solve capability check (Epic #840 Phase 2c, issue #891):** every
 * entry below always renders Phase 1's pre-computed geode-fem export — there
 * is no live-solve UI in this repo yet (Phase 2a/2b/2d are unbuilt; see
 * `docs/design/geode-fem-wasm-webgpu-spike.md`'s DEFER verdict). This section
 * still runs the capability check via `LiveSolveAvailabilityNotice` so the
 * signal (and a clear, non-alarming note when unsupported) is in place for a
 * future live-solve interaction to gate on — see that component's doc
 * comment and `lib/liveSolveCapability.ts`.
 */
export function EmGallerySection() {
  return (
    <section
      id="solver-validation"
      aria-label="Solver Validation"
      className="mt-10 border-t border-border pt-6"
      data-testid="em-gallery-section"
    >
      <h3 className="mb-1 font-mono text-[0.95rem] tracking-[0.05em] text-fog-dim uppercase">
        Solver Validation
      </h3>
      <p className="mb-4 max-w-[44rem] text-[0.85rem] leading-[1.6] text-fog-dim">
        geode-fem, the solver behind each project&rsquo;s Field Data panel, checked
        against known answers: a patch antenna and a spiral inductor against
        closed-form references, plus a real gallery block&rsquo;s interconnect
        coupling against its extracted capacitance. Every result below traces to a
        committed solver run — see each entry&rsquo;s provenance panel for the exact
        solver version, geometry, and solve parameters behind it.
      </p>

      <LiveSolveAvailabilityNotice />

      <div className="flex flex-col gap-4">
        <PatchAntennaGalleryEntry />
        <SpiralInductorGalleryEntry />
        <InterconnectCouplingGalleryEntry />
      </div>
    </section>
  );
}
