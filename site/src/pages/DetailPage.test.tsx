// @vitest-environment jsdom
/**
 * Behavior tests for `DetailPage`'s Downloads section, covering the
 * browser GDS/OAS viewer link added in issue #249: a "View in browser" link
 * to Tiny Tapeout's hosted viewer (Option 1 of that issue — link-out rather
 * than an embedded viewer), gated on the exact same condition as the
 * existing raw-file download link (`layout.downloadable === true &&
 * layout.layout_file !== undefined`).
 */
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { DetailPage } from "./DetailPage";
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

describe("DetailPage viewer link", () => {
  it("renders both the download link and the viewer link when downloadable with a layout_file", () => {
    render(
      <DetailPage
        layout={makeLayout({ downloadable: true, layout_file: "buf_4.gds" })}
      />,
    );

    const downloadLink = screen.getByRole("link", { name: "Layout (buf_4.gds)" });
    expect(downloadLink).toHaveAttribute(
      "href",
      "/blocks/sky130_fd_sc_hd__buf_4/buf_4.gds",
    );

    const viewerLink = screen.getByRole("link", { name: "View in browser" });
    const href = viewerLink.getAttribute("href") ?? "";
    expect(href).toMatch(/^https:\/\/gds-viewer\.tinytapeout\.com\/\?/);

    const params = new URLSearchParams(href.split("?")[1]);
    expect(params.get("model")).toBe(
      "https://klayout-tools.org/blocks/sky130_fd_sc_hd__buf_4/buf_4.gds",
    );
    expect(params.get("pdk")).toBe("sky130A");
    expect(viewerLink).toHaveAttribute("target", "_blank");
    expect(viewerLink).toHaveAttribute("rel", "noopener noreferrer");
  });

  it("omits the viewer link when downloadable is true but layout_file is absent", () => {
    render(<DetailPage layout={makeLayout({ downloadable: true, layout_file: undefined })} />);

    expect(screen.queryByRole("link", { name: "View in browser" })).not.toBeInTheDocument();
    expect(screen.getByText("No download available.")).toBeInTheDocument();
  });

  it("omits the viewer link when downloadable is false", () => {
    render(
      <DetailPage
        layout={makeLayout({ downloadable: false, layout_file: "buf_4.gds" })}
      />,
    );

    expect(screen.queryByRole("link", { name: "View in browser" })).not.toBeInTheDocument();
    expect(screen.getByText("No download available.")).toBeInTheDocument();
  });

  it("omits the viewer link when downloadable/layout_file are both absent (no_artifacts)", () => {
    render(<DetailPage layout={makeLayout({ status: "no_artifacts" })} />);

    expect(screen.queryByRole("link", { name: "View in browser" })).not.toBeInTheDocument();
    expect(screen.getByText("No download available.")).toBeInTheDocument();
  });
});
