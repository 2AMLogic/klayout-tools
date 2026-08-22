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

// Mocked so the "notice stays unmounted" guard below can assert the section
// never even invokes the live-solve capability probe (see that test).
const checkLiveSolveCapability = vi.hoisted(() => vi.fn());
vi.mock("@/lib/liveSolveCapability", () => ({ checkLiveSolveCapability }));

import { EmGallerySection } from "./EmGallerySection";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  checkLiveSolveCapability.mockReset();
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

  it("does not render the live-solve availability notice while no live-solve UI exists (issue #891)", () => {
    // `LiveSolveAvailabilityNotice` is built, exported, and tested, but is
    // deliberately not mounted here: its copy tells the reader live solving
    // isn't available *on this device*, which would be misleading on a site
    // where live solving doesn't exist on any device (Phase 2a/2b/2d unbuilt).
    // Mount it from the live-solve entry point when #890/#892 land — and
    // delete this test in the same change.
    vi.stubGlobal("fetch", vi.fn(() => new Promise(() => {})));
    render(<EmGallerySection />);

    // Asserting the probe was never *invoked* is the deterministic half of
    // this guard: the notice renders nothing until its async check resolves,
    // so a testid-absence assertion alone would pass even if it were mounted.
    expect(checkLiveSolveCapability).not.toHaveBeenCalled();
    expect(screen.queryByTestId("live-solve-unavailable-notice")).not.toBeInTheDocument();
  });
});
