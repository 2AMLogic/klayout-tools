/**
 * Playground engine perf harness (Epic #90 phase B, issue #150).
 *
 * Reproduces the spike's perf methodology (`docs/design/wasm-spice-playground-spike.md`
 * §4) directly against the pinned `eecircuit-engine`: one cold `start()`
 * (one-time WASM instantiation), then two `runSim()` calls on the *same*
 * instance (modeling "edit stimulus, click Re-run"). Prints the timings so the
 * spike's Node-harness numbers can be re-measured on demand, and — with a
 * browser DevTools console running the equivalent `perfProbe()` from
 * `src/lib/playground/engine.ts` — cross-checked against a real browser tab
 * (the spike's "Real-browser perf validation" open question).
 *
 * When a phase-A model deck has been staged (run `npm run build`/`predev` with
 * `pdks/cell-netlists/` populated), this assembles the same transistor-level
 * inverter the spike benchmarked, using the vendored, checksummed models. When
 * no deck is present (fresh checkout, no fetched netlists), it falls back to a
 * self-contained RC deck so `start()` timing — the dominant, corner-independent
 * cost — is still measured.
 *
 * Usage (from the `site/` directory, after `npm install`):
 *     node scripts/playground-perf.mjs
 *     node scripts/playground-perf.mjs --json
 */

import { readFileSync, existsSync, readdirSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { performance } from "node:perf_hooks";
import { Simulation } from "eecircuit-engine";

const SCRIPT_DIR = dirname(fileURLToPath(import.meta.url));
const SITE_DIR = resolve(SCRIPT_DIR, "..");
const BLOCKS_DIR = join(SITE_DIR, "public", "blocks");

/** Inverter testbench mirroring `INVERTER_TESTBENCH_TEMPLATE` in
 * `src/lib/playground/netlist.ts` (kept in sync; this harness is plain JS and
 * can't import the TS module). */
function inverterDeck(modelText) {
  return `* playground perf harness: transistor-level inverter
${modelText.replace(/\s+$/, "")}
VVPWR VPWR 0 DC 1.8
Vin A 0 DC 0 PULSE(0 1.8 0 100p 100p 20n 40n)
XP Y A VPWR VPWR sky130_fd_pr__pfet_01v8_hvt L=0.15 W=1.0
XN Y A 0 0 sky130_fd_pr__nfet_01v8 L=0.15 W=0.65
CL Y 0 5f
.temp 27
.tran 10p 40n
.end
`;
}

/** Self-contained RC fallback when no vendored model deck is staged. */
const RC_DECK = `* playground perf harness: RC fallback (no vendored deck staged)
V1 in 0 DC 0 PULSE(0 1.8 0 100p 100p 20n 40n)
R1 in out 1k
C1 out 0 1p
.tran 10p 40n
.end
`;

/** Find the first staged `tt.spice` model deck, if any. */
function findStagedDeck() {
  if (!existsSync(BLOCKS_DIR)) return null;
  for (const slug of readdirSync(BLOCKS_DIR)) {
    const deck = join(BLOCKS_DIR, slug, "models", "tt.spice");
    if (existsSync(deck)) return { slug, path: deck };
  }
  return null;
}

async function main() {
  const asJson = process.argv.includes("--json");
  const staged = findStagedDeck();
  const netlist = staged ? inverterDeck(readFileSync(staged.path, "utf8")) : RC_DECK;
  const deckKind = staged ? `vendored inverter (${staged.slug})` : "RC fallback";

  const t0 = performance.now();
  const sim = new Simulation();
  await sim.start();
  const t1 = performance.now();

  sim.setNetList(netlist);
  const first = await sim.runSim();
  const t2 = performance.now();

  sim.setNetList(netlist);
  const second = await sim.runSim();
  const t3 = performance.now();

  const ngspice =
    first.header.split(/\r?\n/).find((l) => /ngspice/i.test(l))?.trim() ?? null;

  const report = {
    deck: deckKind,
    engine_version: "1.7.0",
    ngspice: ngspice,
    start_ms: Math.round(t1 - t0),
    first_run_ms: Math.round(t2 - t1),
    second_run_ms: Math.round(t3 - t2),
    num_points: first.numPoints,
    variables: second.variableNames,
  };

  if (asJson) {
    console.log(JSON.stringify(report, null, 2));
    return;
  }

  console.log(`playground engine perf (deck: ${deckKind})`);
  console.log(`  ${ngspice ?? "ngspice version unknown"}`);
  console.log(`  start()      ${report.start_ms} ms  (one-time WASM init)`);
  console.log(`  runSim() #1  ${report.first_run_ms} ms`);
  console.log(`  runSim() #2  ${report.second_run_ms} ms  (same instance, reused)`);
  console.log(`  ${report.num_points} points; vars: ${report.variables.join(", ")}`);
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
