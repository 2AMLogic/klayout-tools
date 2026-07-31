// @vitest-environment jsdom
/**
 * Behavior tests for `LayoutCard` covering the two "honest gallery" rules
 * ported from PR #89 into the React component (issue #136):
 *
 *  1. The status chip is omitted entirely for `"ok"` blocks and rendered
 *     only for `"partial"` / `"no_artifacts"` — a plain, ready block must
 *     not oversell itself with a green "Ready" badge over its thumbnail.
 *  2. `thumbnailFor()` prefers the composite `renders["overview"]` image,
 *     falling back to the first `renders` entry (object-insertion order),
 *     then to the static placeholder when `renders` is absent/empty.
 *
 * Uses `@testing-library/react` (per-file `jsdom` environment, matching
 * `WaveformViewer.test.tsx`; the rest of the site suite is node-environment
 * pure-logic tests).
 */
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { LayoutCard } from "./LayoutCard";
import type { Layout } from "@/data/types";

afterEach(cleanup);

/** Minimal valid `Layout` record; individual tests override fields. */
function makeLayout(overrides: Partial<Layout> = {}): Layout {
  return {
    schema_version: 1,
    generated_at: "2026-07-31T00:00:00Z",
    slug: "sky130_fd_sc_hd__buf_4",
    name: "buf_4",
    status: "ok",
    ...overrides,
  };
}

describe("LayoutCard status chip", () => {
  it('renders no status badge when status is "ok"', () => {
    render(<LayoutCard layout={makeLayout({ status: "ok" })} />);
    expect(screen.queryByText("Ready")).not.toBeInTheDocument();
    expect(screen.queryByText("Partial")).not.toBeInTheDocument();
    expect(screen.queryByText("No artifacts")).not.toBeInTheDocument();
  });

  it('renders the "Partial" badge when status is "partial"', () => {
    render(<LayoutCard layout={makeLayout({ status: "partial" })} />);
    expect(screen.getByText("Partial")).toBeInTheDocument();
  });

  it('renders the "No artifacts" badge when status is "no_artifacts"', () => {
    render(<LayoutCard layout={makeLayout({ status: "no_artifacts" })} />);
    expect(screen.getByText("No artifacts")).toBeInTheDocument();
  });
});

describe("LayoutCard thumbnail selection", () => {
  it('prefers renders["overview"] when present', () => {
    const layout = makeLayout({
      renders: {
        "0_0": "renders/0_0.png",
        overview: "renders/overview.png",
        "112_1": "renders/112_1.png",
      },
    });
    render(<LayoutCard layout={layout} />);
    const img = screen.getByRole("img") as HTMLImageElement;
    expect(img.getAttribute("src")).toBe(
      "/blocks/sky130_fd_sc_hd__buf_4/renders/overview.png",
    );
  });

  it("falls back to the first renders entry when overview is absent", () => {
    const layout = makeLayout({
      renders: {
        "64_16": "renders/64_16.png",
        "236_0": "renders/236_0.png",
      },
    });
    render(<LayoutCard layout={layout} />);
    const img = screen.getByRole("img") as HTMLImageElement;
    expect(img.getAttribute("src")).toBe(
      "/blocks/sky130_fd_sc_hd__buf_4/renders/64_16.png",
    );
  });

  it("falls back to the placeholder when renders is undefined", () => {
    const layout = makeLayout({ status: "no_artifacts", renders: undefined });
    render(<LayoutCard layout={layout} />);
    const img = screen.getByRole("img") as HTMLImageElement;
    expect(img.getAttribute("src")).toBe("/placeholder-layout.svg");
  });

  it("falls back to the placeholder when renders is empty", () => {
    const layout = makeLayout({ renders: {} });
    render(<LayoutCard layout={layout} />);
    const img = screen.getByRole("img") as HTMLImageElement;
    expect(img.getAttribute("src")).toBe("/placeholder-layout.svg");
  });
});
