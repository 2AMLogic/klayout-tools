// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { IndexPage } from "./IndexPage";
import type { Layout } from "@/data/types";

vi.mock("@/components/em", () => ({ EmGallerySection: () => null }));
vi.mock("@/components/LayoutCard", () => ({
  LayoutCard: ({ layout }: { layout: Layout }) => <div data-testid="card">{layout.slug}</div>,
}));

const src = { repo: "2AMLogic/x", ref: "r", path: "." };
const mk = (slug: string, extra: Partial<Layout> = {}): Layout =>
  ({ schema_version: 1, generated_at: "t", slug, name: slug, status: "ok", ...extra }) as Layout;

const layouts = [
  mk("sky130-a", { source: src, pdk: "sky130" }),
  mk("gf180-b", { source: src, status: "partial" }),
  mk("asap7-c", { source: src, status: "in design — simulation evidence" }),
  mk("sky130_fd_sc_hd__inv_1"),
];

afterEach(cleanup);

describe("IndexPage gallery filters", () => {
  it("shows canaries as cards and moves corpus cells to a footnote", () => {
    render(<IndexPage layouts={layouts} />);
    expect(screen.getAllByTestId("card").map((c) => c.textContent)).toEqual([
      "sky130-a",
      "gf180-b",
      "asap7-c",
    ]);
    expect(screen.getByRole("region", { name: "Test corpus" })).toBeTruthy();
  });

  it("filters by PDK, deriving it from the slug when layout.pdk is absent", () => {
    render(<IndexPage layouts={layouts} />);
    fireEvent.click(screen.getByRole("button", { name: "asap7" }));
    expect(screen.getAllByTestId("card").map((c) => c.textContent)).toEqual(["asap7-c"]);
  });

  it("filters by status", () => {
    render(<IndexPage layouts={layouts} />);
    fireEvent.click(screen.getByRole("button", { name: "Partial" }));
    expect(screen.getAllByTestId("card").map((c) => c.textContent)).toEqual(["gf180-b"]);
  });
});
