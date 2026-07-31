/**
 * Unit tests for the layout data loader.
 *
 * These build a temporary `blocks/` fixture tree on disk and point the
 * loader at it via the `KLT_BLOCKS_DIR` override, exercising:
 *   - missing layout.json     -> stub with status "no_artifacts"
 *   - valid layout.json       -> parsed, typed Layout
 *   - unknown schema_version  -> skipped (stub), build does not crash
 *   - hidden/underscore directory skipping
 *   - slug-sorted output
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { discoverBlockDirs, loadLayout, loadLayouts } from "./loadLayouts.ts";

let root: string;

function makeBlockDir(slug: string): string {
  const dir = join(root, slug);
  mkdirSync(join(dir, "output"), { recursive: true });
  return dir;
}

function writeLayoutJson(slug: string, data: unknown): void {
  const dir = makeBlockDir(slug);
  writeFileSync(join(dir, "output", "layout.json"), JSON.stringify(data));
}

const validLayout = (slug: string, overrides: Record<string, unknown> = {}) => ({
  schema_version: 1,
  generated_at: "2026-07-31T00:00:00+00:00",
  slug,
  name: slug,
  status: "ok",
  ...overrides,
});

beforeEach(() => {
  root = mkdtempSync(join(tmpdir(), "klt-blocks-"));
});

afterEach(() => {
  rmSync(root, { recursive: true, force: true });
  vi.restoreAllMocks();
});

describe("discoverBlockDirs", () => {
  it("returns [] when the blocks root does not exist", () => {
    expect(discoverBlockDirs(join(root, "does-not-exist"))).toEqual([]);
  });

  it("lists immediate block dirs", () => {
    makeBlockDir("sky130_fd_sc_hd__inv_1");
    makeBlockDir("sky130_fd_sc_hd__nand2_2");

    const dirs = discoverBlockDirs(root).map((d) => d.replace(root + "/", ""));
    expect(dirs).toEqual(["sky130_fd_sc_hd__inv_1", "sky130_fd_sc_hd__nand2_2"]);
  });

  it("skips hidden and underscore-prefixed directories", () => {
    makeBlockDir("sky130_fd_sc_hd__inv_1");
    mkdirSync(join(root, ".hidden"), { recursive: true });
    mkdirSync(join(root, "_scratch"), { recursive: true });

    const names = discoverBlockDirs(root).map((d) => d.replace(root + "/", ""));
    expect(names).toEqual(["sky130_fd_sc_hd__inv_1"]);
  });
});

describe("loadLayout", () => {
  it("synthesizes a no_artifacts stub when layout.json is absent", () => {
    const dir = makeBlockDir("sky130_fd_sc_hd__inv_1");
    const layout = loadLayout(dir);
    expect(layout.slug).toBe("sky130_fd_sc_hd__inv_1");
    expect(layout.status).toBe("no_artifacts");
    expect(layout.schema_version).toBe(1);
  });

  it("parses a valid layout.json into a typed Layout", () => {
    writeLayoutJson(
      "sky130_fd_sc_hd__dfxtp_2",
      validLayout("sky130_fd_sc_hd__dfxtp_2", {
        name: "sky130_fd_sc_hd__dfxtp_2",
        layout_file: "layout.gds",
        layer_count: 22,
        cell_count: 1,
        instance_count: 0,
        drc: { deck: "sky130", status: "clean", violation_count: 0 },
        renders: { top: "renders/top.png" },
      }),
    );
    const layout = loadLayout(join(root, "sky130_fd_sc_hd__dfxtp_2"));
    expect(layout.status).toBe("ok");
    expect(layout.name).toBe("sky130_fd_sc_hd__dfxtp_2");
    expect(layout.layout_file).toBe("layout.gds");
    expect(layout.layer_count).toBe(22);
    expect(layout.drc?.deck).toBe("sky130");
    expect(layout.drc?.violation_count).toBe(0);
    expect(layout.renders?.top).toBe("renders/top.png");
  });

  it("skips an unknown schema_version and returns a stub, with a warning", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    writeLayoutJson("future-block", validLayout("future-block", { schema_version: 2 }));
    const layout = loadLayout(join(root, "future-block"));
    expect(layout.status).toBe("no_artifacts");
    expect(layout.schema_version).toBe(1); // stub uses the version we understand
    expect(warn).toHaveBeenCalled();
  });

  it("falls back to a stub on invalid JSON, with a warning", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const dir = makeBlockDir("bad-json");
    writeFileSync(join(dir, "output", "layout.json"), "{ not valid json");
    const layout = loadLayout(dir);
    expect(layout.status).toBe("no_artifacts");
    expect(warn).toHaveBeenCalled();
  });

  it("falls back to a stub when a required field is missing", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    writeLayoutJson("missing-status", {
      schema_version: 1,
      generated_at: "2026-07-31T00:00:00+00:00",
      slug: "missing-status",
      name: "missing-status",
      // status omitted
    });
    const layout = loadLayout(join(root, "missing-status"));
    expect(layout.status).toBe("no_artifacts");
    expect(warn).toHaveBeenCalled();
  });
});

describe("loadLayouts", () => {
  it("returns blocks sorted by slug, mixing parsed and stub records", () => {
    writeLayoutJson("sky130_fd_sc_hd__nand2_2", validLayout("sky130_fd_sc_hd__nand2_2"));
    makeBlockDir("sky130_fd_sc_hd__buf_4"); // no layout.json -> stub
    writeLayoutJson(
      "sky130_fd_sc_hd__dfxtp_2",
      validLayout("sky130_fd_sc_hd__dfxtp_2", { status: "partial" }),
    );

    const layouts = loadLayouts(root);
    expect(layouts.map((l) => l.slug)).toEqual([
      "sky130_fd_sc_hd__buf_4",
      "sky130_fd_sc_hd__dfxtp_2",
      "sky130_fd_sc_hd__nand2_2",
    ]);
    expect(layouts[0]?.status).toBe("no_artifacts");
    expect(layouts[1]?.status).toBe("partial");
    expect(layouts[2]?.status).toBe("ok");
  });

  it("returns [] for an empty blocks root (build must still succeed)", () => {
    expect(loadLayouts(root)).toEqual([]);
  });
});
