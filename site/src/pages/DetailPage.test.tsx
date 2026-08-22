// @vitest-environment jsdom
/**
 * Behavior tests for `DetailPage`'s Downloads section and embedded GDS
 * viewer overlay (issue #943), which supersedes the "View in browser" link
 * added in issue #249: a link that navigated away to a third-party hosted
 * viewer in a new tab is now a same-page overlay (`GdsViewer`) opened by
 * clicking either a render thumbnail or the Downloads section's "View in
 * browser" button -- both gated on the exact same condition as the existing
 * raw-file download link (`layout.downloadable === true &&
 * layout.layout_file !== undefined`).
 *
 * The overlay's renderer (`GdsCanvas`, issue #1284) is stubbed here so these
 * page-level tests assert what the page hands it -- the same-origin file
 * URL, the derived PDK family, and the block's own layer names -- rather
 * than re-testing rendering; `GdsCanvas.test.tsx` covers the real renderer
 * against real GDS bytes.
 *
 * Also covers per-PDK-family derivation (issue #1060): the viewer used to
 * hardcode `sky130A` for every block, which gave gf180mcu blocks the wrong
 * layer colors entirely.
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

vi.mock("@/components/layout/GdsCanvas", () => ({
  default: ({
    fileUrl,
    displayName,
    pdkFamily,
    layerNames,
  }: {
    fileUrl: string;
    displayName: string;
    pdkFamily?: string;
    layerNames?: Record<string, string>;
  }) => (
    <div
      data-testid="gds-canvas-stub"
      data-file-url={fileUrl}
      data-display-name={displayName}
      data-pdk-family={pdkFamily ?? ""}
      data-layer-names={JSON.stringify(layerNames ?? {})}
    />
  ),
}));

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

/** Opens the overlay from the Downloads section and returns the renderer stub. */
async function openViewerFromDownloads(): Promise<HTMLElement> {
  fireEvent.click(screen.getByRole("button", { name: "View in browser" }));
  const dialog = screen.getByRole("dialog");
  return within(dialog).findByTestId("gds-canvas-stub");
}

describe("DetailPage GdsViewer overlay (issue #943)", () => {
  it("renders the download link and a 'View in browser' button that opens the embedded viewer when downloadable with a layout_file", async () => {
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

    const canvas = await openViewerFromDownloads();
    expect(
      screen.getByRole("dialog", { name: /Interactive GDS viewer for/ }),
    ).toBeInTheDocument();
    // Same-origin, root-relative: the renderer fetches and draws the file
    // itself, so it must resolve against whatever origin serves the page
    // (issue #1284) rather than a hardcoded production origin.
    expect(canvas).toHaveAttribute("data-file-url", "/blocks/sky130_fd_sc_hd__buf_4/buf_4.gds");
    expect(canvas).toHaveAttribute("data-pdk-family", "sky130");
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

  it("opens the same overlay by clicking a render thumbnail", async () => {
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

    const canvas = await within(screen.getByRole("dialog")).findByTestId("gds-canvas-stub");
    expect(canvas).toHaveAttribute("data-file-url", "/blocks/sky130_fd_sc_hd__buf_4/buf_4.gds");
  });

  it("passes the block's own per-layer render names through to the viewer", async () => {
    render(
      <DetailPage
        layout={makeLayout({
          slug: "gf180-bandgap",
          name: "gf180 Bandgap",
          downloadable: true,
          layout_file: "bandgap_top.gds",
          renders: {
            overview: "renders/overview.png",
            Nwell: "renders/21_0.png",
            Metal1: "renders/34_0.png",
            layer_49_0: "renders/49_0.png",
          },
        })}
      />,
    );

    const canvas = await openViewerFromDownloads();
    expect(JSON.parse(canvas.getAttribute("data-layer-names") ?? "{}")).toEqual({
      "21/0": "Nwell",
      "34/0": "Metal1",
    });
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
  it("styles a gf180mcu std-cell slug with the gf180mcu palette", async () => {
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

    const canvas = await openViewerFromDownloads();
    expect(canvas).toHaveAttribute(
      "data-file-url",
      "/blocks/gf180mcu_fd_sc_mcu9t5v0__and2_1/and2_1.gds",
    );
    expect(canvas).toHaveAttribute("data-pdk-family", "gf180mcu");
  });

  it("styles the gf180-bandgap slug with the gf180mcu palette", async () => {
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

    expect(await openViewerFromDownloads()).toHaveAttribute("data-pdk-family", "gf180mcu");
  });

  it("styles a sky130 slug with the sky130 palette", async () => {
    render(
      <DetailPage
        layout={makeLayout({
          slug: "sky130_fd_sc_hd__nand2_2",
          downloadable: true,
          layout_file: "nand2_2.gds",
        })}
      />,
    );

    expect(await openViewerFromDownloads()).toHaveAttribute("data-pdk-family", "sky130");
  });

  it("styles an sg13g2 slug with the ihp-sg13g2 palette", async () => {
    render(
      <DetailPage
        layout={makeLayout({
          slug: "sg13g2-inv",
          downloadable: true,
          layout_file: "layout.gds",
        })}
      />,
    );

    expect(await openViewerFromDownloads()).toHaveAttribute("data-pdk-family", "ihp-sg13g2");
  });

  it("passes no PDK family (rather than defaulting to sky130) and logs a warning for an unrecognized slug prefix", async () => {
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

    const canvas = await openViewerFromDownloads();
    expect(canvas).toHaveAttribute("data-pdk-family", "");
    expect(canvas).toHaveAttribute("data-file-url", "/blocks/some-future-pdk-block/layout.gds");
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
