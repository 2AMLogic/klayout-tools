/**
 * Unit tests for the field-data (em-export) loader (Epic #840 Phase 3b,
 * issue #959).
 *
 * Mirrors `loadLayouts.test.ts`'s fixture-tree approach: build a temporary
 * block directory on disk and point `loadEmExport` at it directly, exercising:
 *   - missing `<slug>.em-export.json`  -> null (no panel), never throws
 *   - valid em-export.json             -> parsed, typed EmSiteExport
 *   - malformed JSON                   -> null + console.warn, never throws
 *   - unknown schema_version           -> null + console.warn, skipped
 *   - missing required field           -> null + console.warn
 *   - neither s_parameters nor capacitance -> null + console.warn
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { loadEmExport } from "./loadEmExport.ts";

let root: string;

function makeBlockDir(slug: string): string {
  const dir = join(root, slug);
  mkdirSync(join(dir, "output"), { recursive: true });
  return dir;
}

function writeEmExportJson(slug: string, data: unknown): string {
  const dir = makeBlockDir(slug);
  writeFileSync(join(dir, "output", `${slug}.em-export.json`), JSON.stringify(data));
  return dir;
}

const validEmExport = (slug: string, overrides: Record<string, unknown> = {}) => ({
  schema_version: 1,
  benchmark: "block_coupling",
  block: slug,
  mesh: { vertices: [[0, 0, 0]], cells: [[0]] },
  frames: [{ label: "vref driven", frequency_hz: null, scalar: [0] }],
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
  ...overrides,
});

beforeEach(() => {
  root = mkdtempSync(join(tmpdir(), "klt-em-export-"));
});

afterEach(() => {
  rmSync(root, { recursive: true, force: true });
  vi.restoreAllMocks();
});

describe("loadEmExport", () => {
  it("returns null when <slug>.em-export.json is absent (no panel, never throws)", () => {
    const dir = makeBlockDir("sky130_fd_sc_hd__inv_1");
    expect(loadEmExport(dir)).toBeNull();
  });

  it("parses a valid em-export.json into a typed EmSiteExport", () => {
    const dir = writeEmExportJson("gf180-bandgap", validEmExport("gf180-bandgap"));
    const result = loadEmExport(dir);
    expect(result).not.toBeNull();
    expect(result?.schema_version).toBe(1);
    expect(result?.benchmark).toBe("block_coupling");
    expect(result?.mesh.vertices).toEqual([[0, 0, 0]]);
    expect(result?.frames[0]?.label).toBe("vref driven");
    expect(result?.capacitance?.conductors).toEqual(["vgnd", "vpwr"]);
    expect(result?.provenance.generator.commit).toBe("a".repeat(40));
  });

  it("returns null and warns on malformed JSON, without throwing", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const dir = makeBlockDir("bad-json");
    writeFileSync(join(dir, "output", "bad-json.em-export.json"), "{ not valid json");
    expect(loadEmExport(dir)).toBeNull();
    expect(warn).toHaveBeenCalled();
  });

  it("returns null and warns on an unknown schema_version", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const dir = writeEmExportJson("future-block", validEmExport("future-block", { schema_version: 2 }));
    expect(loadEmExport(dir)).toBeNull();
    expect(warn).toHaveBeenCalled();
  });

  it("returns null and warns when a required field is missing", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const dir = makeBlockDir("missing-mesh");
    writeFileSync(
      join(dir, "output", "missing-mesh.em-export.json"),
      JSON.stringify({
        schema_version: 1,
        benchmark: "block_coupling",
        frames: [],
        provenance: { generator: {}, geometry: {}, generated_at: "2026-08-14T00:00:00Z" },
        // mesh omitted
      }),
    );
    expect(loadEmExport(dir)).toBeNull();
    expect(warn).toHaveBeenCalled();
  });

  it("returns null and warns when neither s_parameters nor capacitance is present", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const data = validEmExport("no-anyof");
    delete (data as Record<string, unknown>).capacitance;
    const dir = writeEmExportJson("no-anyof", data);
    expect(loadEmExport(dir)).toBeNull();
    expect(warn).toHaveBeenCalled();
  });

  it("returns null and warns on a non-object payload", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const dir = writeEmExportJson("array-payload", []);
    expect(loadEmExport(dir)).toBeNull();
    expect(warn).toHaveBeenCalled();
  });
});
