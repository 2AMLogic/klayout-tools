// @vitest-environment jsdom
/**
 * Unit tests for `GdsViewer` (issue #943) -- the in-page embedded viewer
 * overlay. `DetailPage.test.tsx` covers the end-to-end entry points (render
 * thumbnails / Downloads section button); this file covers the component's
 * own contract in isolation: `buildViewerSrc()`'s URL shape, `isOpen`
 * gating (nothing rendered, no `document.body` touch, when closed -- the
 * SSR-safety property described in the module doc), and close affordances.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { GdsViewer, buildViewerSrc } from "./GdsViewer";

afterEach(cleanup);

describe("buildViewerSrc", () => {
  it("includes the model param and omits pdk when not supplied", () => {
    const src = buildViewerSrc("https://klayout-tools.org/blocks/x/x.gds");
    expect(src).toBe(
      "https://gds-viewer.tinytapeout.com/?model=https%3A%2F%2Fklayout-tools.org%2Fblocks%2Fx%2Fx.gds",
    );
  });

  it("includes both model and pdk when pdk is supplied", () => {
    const src = buildViewerSrc("https://klayout-tools.org/blocks/x/x.gds", "sky130A");
    const params = new URLSearchParams(src.split("?")[1]);
    expect(params.get("model")).toBe("https://klayout-tools.org/blocks/x/x.gds");
    expect(params.get("pdk")).toBe("sky130A");
  });
});

describe("GdsViewer", () => {
  it("renders nothing when isOpen is false", () => {
    render(
      <GdsViewer
        isOpen={false}
        onClose={() => {}}
        fileUrl="https://klayout-tools.org/blocks/x/x.gds"
        pdk="sky130A"
        displayName="x"
      />,
    );

    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("renders a dialog with an iframe pointed at the viewer src when isOpen is true", () => {
    render(
      <GdsViewer
        isOpen={true}
        onClose={() => {}}
        fileUrl="https://klayout-tools.org/blocks/x/x.gds"
        pdk="sky130A"
        displayName="x"
      />,
    );

    const dialog = screen.getByRole("dialog", { name: "Interactive GDS viewer for x" });
    expect(dialog).toBeInTheDocument();
    const iframe = screen.getByTitle("Interactive GDS viewer for x");
    expect(iframe.tagName).toBe("IFRAME");
    expect(iframe).toHaveAttribute(
      "src",
      buildViewerSrc("https://klayout-tools.org/blocks/x/x.gds", "sky130A"),
    );
  });

  it("calls onClose when the Close button is clicked", () => {
    const onClose = vi.fn();
    render(
      <GdsViewer
        isOpen={true}
        onClose={onClose}
        fileUrl="https://klayout-tools.org/blocks/x/x.gds"
        displayName="x"
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("calls onClose when the backdrop is clicked, but not when the iframe/header is clicked", () => {
    const onClose = vi.fn();
    render(
      <GdsViewer
        isOpen={true}
        onClose={onClose}
        fileUrl="https://klayout-tools.org/blocks/x/x.gds"
        displayName="x"
      />,
    );

    fireEvent.click(screen.getByTitle("Interactive GDS viewer for x"));
    expect(onClose).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("dialog"));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("calls onClose on Escape", () => {
    const onClose = vi.fn();
    render(
      <GdsViewer
        isOpen={true}
        onClose={onClose}
        fileUrl="https://klayout-tools.org/blocks/x/x.gds"
        displayName="x"
      />,
    );

    fireEvent.keyDown(document, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});
