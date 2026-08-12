// @vitest-environment jsdom
/**
 * Render test for `EmGallerySection` (issue #851, extended #877, #842) — the
 * "Electromagnetics" gallery section itself, independent of the
 * fetch-driven benchmark entries it composes (covered by
 * `PatchAntennaGalleryEntry.test.tsx` / `SpiralInductorGalleryEntry.test.tsx` /
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
  it("renders the section heading and all required benchmark entries, including the real-fleet-geometry entry", () => {
    vi.stubGlobal("fetch", vi.fn(() => new Promise(() => {})));
    render(<EmGallerySection />);

    expect(screen.getByTestId("em-gallery-section")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Electromagnetics", level: 2 })).toBeInTheDocument();
    expect(screen.getByTestId("em-benchmark-entry-patch-antenna")).toBeInTheDocument();
    expect(screen.getByTestId("em-benchmark-entry-spiral-inductor")).toBeInTheDocument();
    expect(screen.getByTestId("em-benchmark-entry-interconnect-coupling")).toBeInTheDocument();
  });
});
