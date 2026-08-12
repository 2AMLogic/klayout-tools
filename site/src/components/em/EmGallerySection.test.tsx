// @vitest-environment jsdom
/**
 * Render test for `EmGallerySection` (issue #851) — the "Electromagnetics"
 * gallery section itself, independent of the fetch-driven benchmark entries
 * it composes (covered by `PatchAntennaGalleryEntry.test.tsx`).
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { EmGallerySection } from "./EmGallerySection";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("EmGallerySection", () => {
  it("renders the section heading and at least one benchmark entry", () => {
    vi.stubGlobal("fetch", vi.fn(() => new Promise(() => {})));
    render(<EmGallerySection />);

    expect(screen.getByTestId("em-gallery-section")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Electromagnetics", level: 2 })).toBeInTheDocument();
    expect(screen.getByTestId("em-benchmark-entry-patch-antenna")).toBeInTheDocument();
  });
});
