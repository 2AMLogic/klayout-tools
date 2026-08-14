// @vitest-environment jsdom
/**
 * Render test for `EmGallerySection` (issue #851, extended #877, #842,
 * demoted to a "Solver Validation" strip in Phase 3c/#960) — the section
 * itself, independent of the fetch-driven benchmark entries it composes
 * (covered by `PatchAntennaGalleryEntry.test.tsx` /
 * `SpiralInductorGalleryEntry.test.tsx` /
 * `InterconnectCouplingGalleryEntry.test.tsx`).
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { EmGallerySection } from "./EmGallerySection";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("EmGallerySection", () => {
  it("renders as a subordinate 'Solver Validation' strip with all required benchmark entries, including the real-fleet-geometry entry", () => {
    vi.stubGlobal("fetch", vi.fn(() => new Promise(() => {})));
    render(<EmGallerySection />);

    const section = screen.getByTestId("em-gallery-section");
    expect(section).toBeInTheDocument();
    // Demoted below the h2 "Gallery" heading on IndexPage — h3, not h2 (#960).
    expect(screen.getByRole("heading", { name: "Solver Validation", level: 3 })).toBeInTheDocument();
    // Anchor target for the provenance-trail link from DetailPage's Field Data section.
    expect(section).toHaveAttribute("id", "solver-validation");
    expect(screen.getByTestId("em-benchmark-entry-patch-antenna")).toBeInTheDocument();
    expect(screen.getByTestId("em-benchmark-entry-spiral-inductor")).toBeInTheDocument();
    expect(screen.getByTestId("em-benchmark-entry-interconnect-coupling")).toBeInTheDocument();
  });
});
