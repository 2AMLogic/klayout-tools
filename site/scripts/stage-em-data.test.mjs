import { describe, it, expect, beforeEach, afterEach } from "vitest";
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync, rmSync, existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { stageEmData } from "./stage-em-data.mjs";

describe("stage-em-data", () => {
  let examplesRoot;
  let destRoot;
  let base;

  beforeEach(() => {
    base = mkdtempSync(join(tmpdir(), "klt-stage-em-data-"));
    examplesRoot = join(base, "examples-em");
    destRoot = join(base, "public-em");
  });

  afterEach(() => {
    rmSync(base, { recursive: true, force: true });
  });

  it("stages a benchmark's export JSON byte-for-byte", () => {
    const benchmarkDir = join(examplesRoot, "patch_antenna");
    mkdirSync(benchmarkDir, { recursive: true });
    const payload = JSON.stringify({ schema_version: 1, benchmark: "patch_antenna" });
    writeFileSync(join(benchmarkDir, "patch_antenna.em-export.json"), payload);

    const count = stageEmData(examplesRoot, destRoot);

    expect(count).toBe(1);
    const staged = join(destRoot, "patch_antenna", "patch_antenna.em-export.json");
    expect(existsSync(staged)).toBe(true);
    expect(readFileSync(staged, "utf8")).toBe(payload);
  });

  it("stages multiple benchmarks independently", () => {
    for (const name of ["patch_antenna", "another_bench"]) {
      const dir = join(examplesRoot, name);
      mkdirSync(dir, { recursive: true });
      writeFileSync(join(dir, `${name}.em-export.json`), JSON.stringify({ benchmark: name }));
    }

    const count = stageEmData(examplesRoot, destRoot);
    expect(count).toBe(2);
    expect(existsSync(join(destRoot, "another_bench", "another_bench.em-export.json"))).toBe(true);
  });

  it("skips a benchmark directory with no matching export file", () => {
    mkdirSync(join(examplesRoot, "no_export_yet"), { recursive: true });
    const count = stageEmData(examplesRoot, destRoot);
    expect(count).toBe(0);
  });

  it("is a no-op (not an error) when the examples directory is absent", () => {
    const count = stageEmData(join(base, "does-not-exist"), destRoot);
    expect(count).toBe(0);
    expect(existsSync(destRoot)).toBe(false);
  });

  it("skips hidden/underscore-prefixed entries", () => {
    for (const name of [".hidden", "_scratch"]) {
      const dir = join(examplesRoot, name);
      mkdirSync(dir, { recursive: true });
      writeFileSync(join(dir, `${name}.em-export.json`), "{}");
    }
    const count = stageEmData(examplesRoot, destRoot);
    expect(count).toBe(0);
  });
});
