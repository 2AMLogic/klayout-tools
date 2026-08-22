// @vitest-environment jsdom
/**
 * Behavior tests for `DetailPage`'s Downloads section and embedded GDS
 * viewer overlay (issue #943), which supersedes the "View in browser" link
 * added in issue #249: a link to Tiny Tapeout's hosted viewer that
 * navigated away to a new tab is now a same-page overlay (`GdsViewer`)
 * opened by clicking either a render thumbnail or the Downloads section's
 * "View in browser" button -- both gated on the exact same condition as the
 * existing raw-file download link (`layout.downloadable === true &&
 * layout.layout_file !== undefined`).
 *
 * Also covers per-PDK-family `pdk=` derivation feeding the overlay's
 * `<iframe src>` (issue #1060): the viewer used to hardcode `pdk=sky130A`
 * for every block, which broke the viewer for gf180mcu blocks (wrong layer
 * table, empty render).
 *
 * Also covers the Signals section's canary-block degradation (issue #653):
 * when `layout.signals` carries corners with no `waveform` artifact (e.g.
 * the sky130-bandgap/gf180-bandgap canary blocks), the page must render the
 * static measurements table `WaveformViewer` falls back to, not a
 * corner-toggle fieldset wired to a plot that can never draw.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
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

describe("DetailPage GdsViewer overlay (issue #943)", () => {
  it("renders the download link and a 'View in browser' button that opens the embedded viewer when downloadable with a layout_file", () => {
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

    // No overlay until the entry point is clicked -- never rendered eagerly.
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();

    const viewerButton = screen.getByRole("button", { name: "View in browser" });
    fireEvent.click(viewerButton);

    const dialog = screen.getByRole("dialog", { name: /Interactive GDS viewer for/ });
    const iframe = within(dialog).getByTitle(/Interactive GDS viewer for/);
    const src = iframe.getAttribute("src") ?? "";
    expect(src).toMatch(/^https:\/\/gds-viewer\.tinytapeout\.com\/\?/);

    const params = new URLSearchParams(src.split("?")[1]);
    expect(params.get("model")).toBe(
      "https://klayout-tools.org/blocks/sky130_fd_sc_hd__buf_4/buf_4.gds",
    );
    expect(params.get("pdk")).toBe("sky130A");
  });

  it("closes the overlay via the Close button", () => {
    render(
      <DetailPage layout={makeLayout({ downloadable: true, layout_file: "buf_4.gds" })} />,
    );

    fireEvent.click(screen.getByRole("button", { name: "View in browser" }));
    expect(screen.getByRole("dialog")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("closes the overlay on Escape", () => {
    render(
      <DetailPage layout={makeLayout({ downloadable: true, layout_file: "buf_4.gds" })} />,
    );

    fireEvent.click(screen.getByRole("button", { name: "View in browser" }));
    expect(screen.getByRole("dialog")).toBeInTheDocument();

    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("opens the same overlay by clicking a render thumbnail", () => {
    render(
      <DetailPage
        layout={makeLayout({
          downloadable: true,
          layout_file: "buf_4.gds",
          renders: { overview: "renders/overview.png" },
        })}
      />,
    );

    expect(
      screen.getByText("Click a render to open an interactive, pan/zoom viewer of the actual GDS."),
    ).toBeInTheDocument();

    fireEvent.click(
      screen.getByRole("button", { name: "Open interactive viewer for the overview render of buf_4" }),
    );

    const dialog = screen.getByRole("dialog");
    const iframe = within(dialog).getByTitle(/Interactive GDS viewer for/);
    const params = new URLSearchParams((iframe.getAttribute("src") ?? "").split("?")[1]);
    expect(params.get("model")).toBe(
      "https://klayout-tools.org/blocks/sky130_fd_sc_hd__buf_4/buf_4.gds",
    );
  });

  it("render thumbnails are not clickable (no button role, no hint text) when the block has no downloadable layout file", () => {
    render(
      <DetailPage
        layout={makeLayout({
          renders: { overview: "renders/overview.png" },
        })}
      />,
    );

    expect(
      screen.queryByText(/Click a render to open an interactive/),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /Open interactive viewer/ }),
    ).not.toBeInTheDocument();
  });

  it("omits the 'View in browser' button when downloadable is true but layout_file is absent", () => {
    render(<DetailPage layout={makeLayout({ downloadable: true, layout_file: undefined })} />);

    expect(screen.queryByRole("button", { name: "View in browser" })).not.toBeInTheDocument();
    expect(screen.getByText("No download available.")).toBeInTheDocument();
  });

  it("omits the 'View in browser' button when downloadable is false", () => {
    render(
      <DetailPage
        layout={makeLayout({ downloadable: false, layout_file: "buf_4.gds" })}
      />,
    );

    expect(screen.queryByRole("button", { name: "View in browser" })).not.toBeInTheDocument();
    expect(screen.getByText("No download available.")).toBeInTheDocument();
  });

  it("omits the 'View in browser' button when downloadable/layout_file are both absent (no_artifacts)", () => {
    render(<DetailPage layout={makeLayout({ status: "no_artifacts" })} />);

    expect(screen.queryByRole("button", { name: "View in browser" })).not.toBeInTheDocument();
    expect(screen.getByText("No download available.")).toBeInTheDocument();
  });
});

describe("DetailPage GdsViewer PDK family derivation (issue #1060, carried forward by #943)", () => {
  function openViewerAndGetSrcParams(): URLSearchParams {
    fireEvent.click(screen.getByRole("button", { name: "View in browser" }));
    const dialog = screen.getByRole("dialog");
    const iframe = within(dialog).getByTitle(/Interactive GDS viewer for/);
    const src = iframe.getAttribute("src") ?? "";
    return new URLSearchParams(src.split("?")[1]);
  }

  it("emits pdk=gf180mcuD for a gf180mcu std-cell slug", () => {
    render(
      <DetailPage
        layout={makeLayout({
          slug: "gf180mcu_fd_sc_mcu9t5v0__and2_1",
          name: "and2_1",
          downloadable: true,
          layout_file: "and2_1.gds",
        })}
      />,
    );

    const params = openViewerAndGetSrcParams();
    expect(params.get("model")).toBe(
      "https://klayout-tools.org/blocks/gf180mcu_fd_sc_mcu9t5v0__and2_1/and2_1.gds",
    );
    // Well-formed viewer URL for a real gf180 block slug (acceptance
    // criterion #2 -- full visual verification in the actual viewer was
    // not possible in this headless environment, see PR description).
    expect(params.get("pdk")).toBe("gf180mcuD");
  });

  it("emits pdk=gf180mcuD for the gf180-bandgap slug", () => {
    render(
      <DetailPage
        layout={makeLayout({
          slug: "gf180-bandgap",
          name: "gf180 Bandgap",
          downloadable: true,
          layout_file: "layout.gds",
        })}
      />,
    );

    expect(openViewerAndGetSrcParams().get("pdk")).toBe("gf180mcuD");
  });

  it("emits pdk=sky130A for a sky130 slug", () => {
    render(
      <DetailPage
        layout={makeLayout({
          slug: "sky130_fd_sc_hd__nand2_2",
          downloadable: true,
          layout_file: "nand2_2.gds",
        })}
      />,
    );

    expect(openViewerAndGetSrcParams().get("pdk")).toBe("sky130A");
  });

  it("emits pdk=ihp-sg13g2 for an sg13g2 slug", () => {
    render(
      <DetailPage
        layout={makeLayout({
          slug: "sg13g2-inv",
          downloadable: true,
          layout_file: "layout.gds",
        })}
      />,
    );

    expect(openViewerAndGetSrcParams().get("pdk")).toBe("ihp-sg13g2");
  });

  it("omits the pdk param (rather than defaulting to sky130A) and logs a warning for an unrecognized slug prefix", () => {
    const warnSpy = vi.spyOn(console, "warn").mockImplementation(() => {});

    render(
      <DetailPage
        layout={makeLayout({
          slug: "some-future-pdk-block",
          downloadable: true,
          layout_file: "layout.gds",
        })}
      />,
    );

    const params = openViewerAndGetSrcParams();
    expect(params.has("pdk")).toBe(false);
    expect(params.get("model")).toBe(
      "https://klayout-tools.org/blocks/some-future-pdk-block/layout.gds",
    );
    expect(warnSpy).toHaveBeenCalledWith(expect.stringContaining("some-future-pdk-block"));

    warnSpy.mockRestore();
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
    // Provenance-trail link back to the index page's solver-validation strip (#960).
    expect(screen.getByRole("link", { name: "solver validation" })).toHaveAttribute(
      "href",
      "/#solver-validation",
    );
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

describe("DetailPage Schematic section (issue #1121)", () => {
  it("renders the diagram and provenance line when layout.schematic is present", () => {
    render(
      <DetailPage
        layout={makeLayout({
          schematic: {
            path: "schematic.svg",
            provenance: "Drawn from sky130_fd_sc_hd__inv_1.spice @ 9f8e7d6",
          },
        })}
      />,
    );

    expect(screen.getByRole("heading", { name: "Schematic" })).toBeInTheDocument();
    const img = screen.getByAltText("Schematic diagram of buf_4");
    expect(img).toHaveAttribute("src", "/blocks/sky130_fd_sc_hd__buf_4/schematic.svg");
    expect(
      screen.getByText("Drawn from sky130_fd_sc_hd__inv_1.spice @ 9f8e7d6"),
    ).toBeInTheDocument();
  });

  it("omits the Schematic section entirely when layout.schematic is absent", () => {
    render(<DetailPage layout={makeLayout({ schematic: undefined })} />);

    expect(screen.queryByRole("heading", { name: "Schematic" })).not.toBeInTheDocument();
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
