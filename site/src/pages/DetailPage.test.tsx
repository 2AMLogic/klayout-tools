// @vitest-environment jsdom
/**
 * Behavior tests for `DetailPage`'s Downloads section, covering the
 * browser GDS/OAS viewer link added in issue #249: a "View in browser" link
 * to Tiny Tapeout's hosted viewer (Option 1 of that issue — link-out rather
 * than an embedded viewer), gated on the exact same condition as the
 * existing raw-file download link (`layout.downloadable === true &&
 * layout.layout_file !== undefined`).
 *
 * Also covers the Signals section's canary-block degradation (issue #653):
 * when `layout.signals` carries corners with no `waveform` artifact (e.g.
 * the sky130-bandgap/gf180-bandgap canary blocks), the page must render the
 * static measurements table `WaveformViewer` falls back to, not a
 * corner-toggle fieldset wired to a plot that can never draw.
 */
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { DetailPage } from "./DetailPage";
import type { Layout, LayoutSignals } from "@/data/types";
import type { EmSiteExport } from "@/components/em/types";

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

describe("DetailPage Signals section (issue #653)", () => {
  // Shaped like a canary block's `signals`: real per-corner measurement
  // data, but no corner carries a `waveform` artifact (no `signals/`
  // directory staged for these blocks at all).
  const noTraceSignals: LayoutSignals = {
    schema_version: 1,
    engine: "ngspice",
    engine_version: "46",
    status: "pass",
    corner_count: 1,
    default_corner_id: "tt/3.300V/27C",
    passed: 1,
    failed: 0,
    errored: 0,
    measurements: [],
    corners: [
      {
        corner_id: "tt/3.300V/27C",
        process: "tt",
        supply_v: { vdd: 3.3 },
        temperature_c: 27,
        status: "pass",
        runtime_s: 1.2,
        measurements: [{ name: "vref", value: 1.2, unit: "V", status: "pass", margin: 0.02 }],
        diagnostics: [],
      },
    ],
  };

  it("renders the static measurements table, not a corner-toggle + plot, for a bandgap-style canary block", () => {
    render(
      <DetailPage layout={makeLayout({ slug: "sky130-bandgap", signals: noTraceSignals })} />,
    );

    expect(screen.getByRole("heading", { name: "Signals" })).toBeInTheDocument();
    expect(screen.queryByLabelText(/Toggle corner/)).not.toBeInTheDocument();
    expect(screen.queryByTestId("waveform-plot")).not.toBeInTheDocument();
    expect(screen.getByTestId("signals-measurements-table")).toBeInTheDocument();
    expect(screen.getByText("tt/3.300V/27C")).toBeInTheDocument();
  });

  it("omits the Signals section entirely when layout.signals is absent", () => {
    render(<DetailPage layout={makeLayout({ signals: undefined })} />);

    expect(screen.queryByRole("heading", { name: "Signals" })).not.toBeInTheDocument();
  });
});

describe("DetailPage Field Data section (Epic #840 Phase 3b, issue #959)", () => {
  const emExport: EmSiteExport = {
    schema_version: 1,
    benchmark: "block_coupling",
    mesh: { vertices: [[0, 0, 0], [1, 0, 0], [0, 1, 0]], cells: [[0, 1, 2]] },
    frames: [{ label: "vref driven 1V", frequency_hz: null, scalar: [0, 0.5, 1] }],
    capacitance: {
      conductors: ["vgnd", "vpwr"],
      matrix_farad: [
        [1e-15, -2e-16],
        [-2e-16, 1e-15],
      ],
    },
    provenance: {
      generator: { repo: "https://github.com/2AMLogic/geode-fem", commit: "a".repeat(40) },
      geometry: { fixture: "gf180-bandgap" },
      generated_at: "2026-08-14T00:00:00Z",
    },
  };

  it("renders the field panel with a provenance panel when emExport is present", () => {
    render(<DetailPage layout={makeLayout({ slug: "gf180-bandgap" })} emExport={emExport} />);

    expect(screen.getByRole("heading", { name: "Field Data" })).toBeInTheDocument();
    expect(screen.getByTestId("field-viewer")).toBeInTheDocument();
    expect(screen.getByTestId("em-provenance-panel")).toBeInTheDocument();
  });

  it("omits the Field Data section entirely when emExport is null (no artifact / malformed artifact)", () => {
    render(<DetailPage layout={makeLayout({ slug: "sky130_fd_sc_hd__buf_4" })} emExport={null} />);

    expect(screen.queryByRole("heading", { name: "Field Data" })).not.toBeInTheDocument();
    expect(screen.queryByTestId("field-viewer")).not.toBeInTheDocument();
    expect(screen.queryByTestId("em-provenance-panel")).not.toBeInTheDocument();
  });

  it("omits the Field Data section when emExport is undefined (default, matches every other block)", () => {
    render(<DetailPage layout={makeLayout()} />);

    expect(screen.queryByRole("heading", { name: "Field Data" })).not.toBeInTheDocument();
  });
});

describe("DetailPage Renders section (issue #942)", () => {
  it("shows the overview as a hero image, groups the rest below it with human-readable captions", () => {
    render(
      <DetailPage
        layout={makeLayout({
          renders: {
            overview: "renders/overview.png",
            "poly.drawing": "renders/66_20.png",
            layer_69_44: "renders/69_44.png",
            center_crop: "renders/center_crop/overview.png",
          },
        })}
      />,
    );

    expect(screen.getByText("Overview")).toBeInTheDocument();
    expect(screen.getByText("poly.drawing")).toBeInTheDocument();
    // A numeric-fallback id is reformatted as "Layer N/M" -- never shown
    // as the bare `69_44`-style id.
    expect(screen.getByText("Layer 69/44")).toBeInTheDocument();
    expect(screen.queryByText("69_44", { exact: true })).not.toBeInTheDocument();
    expect(screen.getByText("Zoomed crop")).toBeInTheDocument();

    const overviewImg = screen.getByAltText("Overview render of buf_4");
    expect(overviewImg).toHaveAttribute(
      "src",
      "/blocks/sky130_fd_sc_hd__buf_4/renders/overview.png",
    );
  });

  it("shows the placeholder text when no renders are present", () => {
    render(<DetailPage layout={makeLayout({ renders: undefined })} />);

    expect(screen.getByText("No renders yet.")).toBeInTheDocument();
  });
});
