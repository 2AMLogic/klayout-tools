// @vitest-environment jsdom
/**
 * Unit tests for `GdsViewer` (issue #943 / #1284) -- the in-page viewer
 * overlay. `DetailPage.test.tsx` covers the end-to-end entry points (render
 * thumbnails / Downloads section button) and `GdsCanvas.test.tsx` covers the
 * renderer; this file covers the overlay's own contract: `isOpen` gating
 * (nothing rendered, no `document.body` touch, when closed -- the SSR-safety
 * property described in the module doc), the lazily-loaded renderer, and the
 * three close affordances.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { GdsViewer } from "./GdsViewer";
import { buildTwoLevelFixture } from "@/lib/gds/gdsFixture";

beforeEach(() => {
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(null);
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      statusText: "OK",
      arrayBuffer: async () => buildTwoLevelFixture(),
    } as unknown as Response),
  );
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("GdsViewer", () => {
  it("renders nothing when isOpen is false", () => {
    render(
      <GdsViewer
        isOpen={false}
        onClose={() => {}}
        fileUrl="/blocks/x/x.gds"
        pdkFamily="sky130"
        displayName="x"
      />,
    );

    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("does not fetch the layout until it is opened", () => {
    const { rerender } = render(
      <GdsViewer isOpen={false} onClose={() => {}} fileUrl="/blocks/x/x.gds" displayName="x" />,
    );
    expect(fetch).not.toHaveBeenCalled();

    rerender(
      <GdsViewer isOpen={true} onClose={() => {}} fileUrl="/blocks/x/x.gds" displayName="x" />,
    );
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });

  it("renders the same-origin canvas renderer, not a third-party iframe", async () => {
    const { container } = render(
      <GdsViewer
        isOpen={true}
        onClose={() => {}}
        fileUrl="/blocks/x/x.gds"
        pdkFamily="sky130"
        displayName="x"
      />,
    );

    const dialog = screen.getByRole("dialog", { name: "Interactive GDS viewer for x" });
    expect(dialog).toBeInTheDocument();

    // The renderer arrives through `lazy()`, so it is a suspense boundary
    // away -- which is exactly the "loads lazily" acceptance criterion.
    expect(await screen.findByTestId("gds-canvas-summary")).toBeInTheDocument();
    expect(document.querySelector("iframe")).toBeNull();
    expect(container.querySelector("iframe")).toBeNull();
    expect(fetch).toHaveBeenCalledWith("/blocks/x/x.gds", expect.anything());
  });

  it("calls onClose when the Close button is clicked", () => {
    const onClose = vi.fn();
    render(
      <GdsViewer isOpen={true} onClose={onClose} fileUrl="/blocks/x/x.gds" displayName="x" />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("calls onClose when the backdrop is clicked, but not the header or viewer body", async () => {
    const onClose = vi.fn();
    render(
      <GdsViewer isOpen={true} onClose={onClose} fileUrl="/blocks/x/x.gds" displayName="x" />,
    );

    fireEvent.click(screen.getByText("x — GDS viewer"));
    expect(onClose).not.toHaveBeenCalled();

    fireEvent.click(await screen.findByTestId("gds-canvas-layers"));
    expect(onClose).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("dialog"));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("calls onClose on Escape", () => {
    const onClose = vi.fn();
    render(
      <GdsViewer isOpen={true} onClose={onClose} fileUrl="/blocks/x/x.gds" displayName="x" />,
    );

    fireEvent.keyDown(document, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});
