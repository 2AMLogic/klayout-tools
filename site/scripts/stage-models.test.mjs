import { describe, it, expect, beforeEach, afterEach } from "vitest";
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync, rmSync, existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import {
  sha256Hex,
  assembleCornerDeck,
  stageModelsForBlock,
  SKY130,
} from "./stage-models.mjs";

// Minimal synthetic sky130 model fixtures — the same file *shape* the real
// fetched decks have (a corner file that relative-`.include`s its pm3 binned
// models), tiny enough to keep the test hermetic and fast. We do NOT depend on
// scripts/fetch-cell-netlists.sh having run.
function writeFixtures(netlistsRoot) {
  const modelsDir = join(netlistsRoot, "sky130", "models");
  mkdirSync(modelsDir, { recursive: true });
  for (const dev of SKY130.devices) {
    writeFileSync(
      join(modelsDir, `sky130_fd_pr__${dev}__mismatch.corner.spice`),
      `* ${dev} mismatch\n.param ${dev}_toxe_slope=1.0\n`,
    );
    for (const corner of SKY130.corners) {
      writeFileSync(
        join(modelsDir, `sky130_fd_pr__${dev}__${corner}.corner.spice`),
        `* ${dev} ${corner} corner params\n.param ${dev}_${corner}_mult=1.0\n` +
          `.include "sky130_fd_pr__${dev}__${corner}.pm3.spice"\n`,
      );
      writeFileSync(
        join(modelsDir, `sky130_fd_pr__${dev}__${corner}.pm3.spice`),
        `* ${dev} ${corner} binned models\n.subckt sky130_fd_pr__${dev} d g s b\n.ends\n`,
      );
    }
  }
  return modelsDir;
}

describe("stage-models sky130 deck staging", () => {
  let netlistsRoot;
  let destRoot;

  beforeEach(() => {
    const base = mkdtempSync(join(tmpdir(), "klt-stage-models-"));
    netlistsRoot = join(base, "cell-netlists");
    destRoot = join(base, "public-blocks");
  });

  afterEach(() => {
    // base is the common parent of both roots.
    rmSync(join(netlistsRoot, ".."), { recursive: true, force: true });
  });

  it("stages a deck per corner with a verifiable checksum manifest", () => {
    writeFixtures(netlistsRoot);
    const slug = SKY130.slugs[0];

    const count = stageModelsForBlock(slug, netlistsRoot, destRoot);
    expect(count).toBe(SKY130.corners.length);

    const outDir = join(destRoot, slug, "models");
    const manifest = JSON.parse(readFileSync(join(outDir, "checksums.json"), "utf8"));
    expect(manifest.pdk).toBe("sky130");
    expect(manifest.license).toBe("Apache-2.0");
    expect(Object.keys(manifest.decks).sort()).toEqual([...SKY130.corners].sort());

    for (const corner of SKY130.corners) {
      const entry = manifest.decks[corner];
      expect(entry.path).toBe(`models/${corner}.spice`);

      // Published deck checksum verifies against the staged file's bytes.
      const staged = readFileSync(join(destRoot, slug, entry.path));
      expect(sha256Hex(staged)).toBe(entry.sha256);
      expect(staged.length).toBe(entry.bytes);

      // Every published source checksum verifies against the real
      // pdks/cell-netlists/ source file it names.
      expect(entry.sources.length).toBeGreaterThan(0);
      for (const src of entry.sources) {
        const raw = readFileSync(join(netlistsRoot, src.path));
        expect(sha256Hex(raw)).toBe(src.sha256);
        expect(raw.length).toBe(src.bytes);
      }

      // The pm3 relative include is resolved inline — no dangling .include, and
      // both devices' binned-model subckts are present in the self-contained deck.
      const text = staged.toString("utf8");
      expect(text).not.toMatch(/^\s*\.include/im);
      for (const dev of SKY130.devices) {
        expect(text).toContain(`.subckt sky130_fd_pr__${dev}`);
      }
    }
  });

  it("records each device's mismatch, corner, and inlined pm3 file as sources", () => {
    const modelsDir = writeFixtures(netlistsRoot);
    const deck = assembleCornerDeck(modelsDir, "tt", netlistsRoot);
    expect(deck).not.toBeNull();
    // 3 files per device (mismatch + corner + pm3) x 2 devices.
    expect(deck.sources.length).toBe(SKY130.devices.length * 3);
    const names = deck.sources.map((s) => s.path);
    expect(names).toContain("sky130/models/sky130_fd_pr__nfet_01v8__tt.pm3.spice");
    expect(names).toContain("sky130/models/sky130_fd_pr__pfet_01v8_hvt__mismatch.corner.spice");
  });

  it("is a no-op when the fetched netlists are absent (fresh checkout)", () => {
    // netlistsRoot exists but has no sky130/models subtree.
    mkdirSync(netlistsRoot, { recursive: true });
    const count = stageModelsForBlock(SKY130.slugs[0], netlistsRoot, destRoot);
    expect(count).toBe(0);
    expect(existsSync(join(destRoot, SKY130.slugs[0]))).toBe(false);
  });

  it("stages nothing if only a partial model set is present", () => {
    const modelsDir = writeFixtures(netlistsRoot);
    // Remove one required file — staging must produce nothing, not a partial deck.
    rmSync(join(modelsDir, "sky130_fd_pr__nfet_01v8__ff.corner.spice"));
    const count = stageModelsForBlock(SKY130.slugs[0], netlistsRoot, destRoot);
    expect(count).toBe(0);
    expect(existsSync(join(destRoot, SKY130.slugs[0], "models"))).toBe(false);
  });
});
