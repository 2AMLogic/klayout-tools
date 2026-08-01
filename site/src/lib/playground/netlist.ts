/**
 * Netlist assembly for the in-browser SPICE playground (Epic #90 phase B,
 * issue #150 — parent #148).
 *
 * This module is deliberately **pure** (no `eecircuit-engine`, no `fetch`, no
 * DOM): it turns a set of editable stimulus parameters plus a repo-vendored,
 * checksummed model deck (phase A, issue #149 — staged at
 * `/blocks/<slug>/models/<corner>.spice`) into a single self-contained SPICE
 * deck ready to hand to `Simulation.setNetList()`. Keeping it pure makes the
 * stimulus-parameter -> netlist substitution unit-testable without loading the
 * ~19 MB WASM engine (see `netlist.test.ts`), and lets `engine.ts` own all the
 * lazy-load / cache / I/O concerns separately.
 *
 * Design decisions, per the spike (`docs/design/wasm-spice-playground-spike.md`
 * §3, "Recommended scope boundary"):
 *
 *   - **Vendored models, not the engine's baked-in blobs.** The assembled deck
 *     splices in the phase-A model text verbatim (passed in as `modelText`);
 *     it never `.lib`/`.include`s `eecircuit-engine`'s bundled `nfet18.ts` etc.
 *     Provenance stays with the checksummed phase-A deck.
 *   - **Parameterized stimulus, not free-form netlist text.** Only the
 *     stimulus knobs the spike scoped as editable (supply, pulse slew/timing,
 *     load cap, temperature, transient window) are substituted, via `{{name}}`
 *     placeholders in a testbench template. An unknown placeholder throws, so a
 *     malformed template is caught at assembly time rather than as a cryptic
 *     ngspice parse error.
 *   - **Transistor-level inverter as the default testbench.** The default
 *     template instantiates exactly the two sky130 primitives the phase-A decks
 *     define (`sky130_fd_pr__nfet_01v8` / `sky130_fd_pr__pfet_01v8_hvt`) — the
 *     same netlist the spike benchmarked (§4) — so the deck is self-contained
 *     from the vendored models alone, with no un-staged cell subckt dependency.
 *     Phase C may pass its own `testbenchTemplate` for other gallery cells.
 */

/** Editable stimulus parameters for a playground re-run. All SI base units. */
export interface StimulusParams {
  /** Supply voltage (VPWR rail), volts. */
  supplyV: number;
  /** Input pulse rise time / slew, seconds. */
  riseS: number;
  /** Input pulse fall time / slew, seconds. */
  fallS: number;
  /** Input pulse high (pulse-width) time, seconds. */
  pulseWidthS: number;
  /** Input pulse period, seconds. */
  periodS: number;
  /** Output load capacitance, farads. */
  loadCapF: number;
  /** Simulation temperature, degrees Celsius. */
  temperatureC: number;
  /** Transient-analysis stop time, seconds. */
  stopTimeS: number;
  /** Transient-analysis time step, seconds. */
  stepS: number;
}

/**
 * Nominal defaults for the sky130 inverter testbench, mirroring the spike's
 * benchmark netlist (`.tran 10p 40n` on an `nfet_01v8`/`pfet_01v8_hvt`
 * inverter). Phase C's UI seeds its editable controls from these.
 */
export const DEFAULT_STIMULUS: StimulusParams = {
  supplyV: 1.8,
  riseS: 100e-12,
  fallS: 100e-12,
  pulseWidthS: 20e-9,
  periodS: 40e-9,
  loadCapF: 5e-15,
  temperatureC: 27,
  stopTimeS: 40e-9,
  stepS: 10e-12,
};

/**
 * Default testbench: a transistor-level inverter built from the two vendored
 * sky130 primitives, driven by a parameterized `PULSE` source and loaded by a
 * parameterized cap. Uses `{{name}}` placeholders resolved by `renderTemplate`.
 * The device model cards themselves come from the spliced-in `modelText`, never
 * from the engine's bundled models.
 */
export const INVERTER_TESTBENCH_TEMPLATE = `* transistor-level inverter testbench for {{cell}} (corner {{corner}})
VVPWR VPWR 0 DC {{supplyV}}
Vin A 0 DC 0 PULSE(0 {{supplyV}} 0 {{riseS}} {{fallS}} {{pulseWidthS}} {{periodS}})
XP Y A VPWR VPWR sky130_fd_pr__pfet_01v8_hvt L=0.15 W=1.0
XN Y A 0 0 sky130_fd_pr__nfet_01v8 L=0.15 W=0.65
CL Y 0 {{loadCapF}}`;

/** Ascending (value, ngspice-suffix) SI table covering the f..k range the
 * stimulus knobs span. `m` is milli (ngspice uses `meg` for mega, which the
 * stimulus values never reach). */
const SI_PREFIXES: ReadonlyArray<readonly [number, string]> = [
  [1e-15, "f"],
  [1e-12, "p"],
  [1e-9, "n"],
  [1e-6, "u"],
  [1e-3, "m"],
  [1, ""],
  [1e3, "k"],
];

/** Trim a fixed-precision string of trailing zeros / dangling decimal point. */
function trimZeros(s: string): string {
  return s.includes(".") ? s.replace(/0+$/, "").replace(/\.$/, "") : s;
}

/**
 * Format a plain decimal value (voltage, temperature) with up to 6 significant
 * digits, no SI suffix. `1.8 -> "1.8"`, `27 -> "27"`, `-40 -> "-40"`.
 */
export function formatPlain(value: number): string {
  if (!Number.isFinite(value)) {
    throw new Error(`cannot format non-finite value: ${value}`);
  }
  if (value === 0) return "0";
  return trimZeros(value.toPrecision(6));
}

/**
 * Format a value with an SI suffix chosen so the mantissa lands in `[1, 1000)`.
 * `100e-12 -> "100p"`, `40e-9 -> "40n"`, `5e-15 -> "5f"`, `0 -> "0"`.
 */
export function formatSiValue(value: number): string {
  if (!Number.isFinite(value)) {
    throw new Error(`cannot format non-finite value: ${value}`);
  }
  if (value === 0) return "0";
  const neg = value < 0;
  const abs = Math.abs(value);
  // Pick the largest prefix not exceeding `abs` (comparing magnitudes directly
  // avoids the float-division rounding that makes e.g. 1e-6/1e-9 land at 1000n
  // instead of 1u). The tiny epsilon tolerates exact-boundary inputs.
  let chosen = SI_PREFIXES[0];
  for (const prefix of SI_PREFIXES) {
    if (abs >= prefix[0] * (1 - 1e-12)) chosen = prefix;
    else break;
  }
  const mantissa = trimZeros((abs / chosen[0]).toPrecision(6));
  return `${neg ? "-" : ""}${mantissa}${chosen[1]}`;
}

/**
 * Build the `{{name}} -> string` substitution map for a set of stimulus
 * parameters. Times / capacitance use SI suffixes; voltage / temperature use
 * plain decimals (more natural for a `DC`/`.temp` value).
 */
export function stimulusVars(params: StimulusParams): Record<string, string> {
  return {
    supplyV: formatPlain(params.supplyV),
    riseS: formatSiValue(params.riseS),
    fallS: formatSiValue(params.fallS),
    pulseWidthS: formatSiValue(params.pulseWidthS),
    periodS: formatSiValue(params.periodS),
    loadCapF: formatSiValue(params.loadCapF),
    temperatureC: formatPlain(params.temperatureC),
    stopTimeS: formatSiValue(params.stopTimeS),
    stepS: formatSiValue(params.stepS),
  };
}

/**
 * Substitute every `{{name}}` placeholder in `template` from `vars`. Throws on
 * a placeholder with no matching var (catches template typos at assembly time).
 */
export function renderTemplate(template: string, vars: Record<string, string>): string {
  return template.replace(/\{\{\s*(\w+)\s*\}\}/g, (_match, key: string) => {
    if (!Object.prototype.hasOwnProperty.call(vars, key)) {
      throw new Error(`unknown netlist placeholder {{${key}}}`);
    }
    return vars[key];
  });
}

/** Inputs to {@link assembleNetlist}. */
export interface AssembleNetlistOptions {
  /** Cell name (for the deck title comment), e.g. `sky130_fd_sc_hd__inv_1`. */
  cell: string;
  /** Process corner label (`tt`/`ss`/`ff`), for the title comment. */
  corner: string;
  /** Editable stimulus parameters. */
  params: StimulusParams;
  /**
   * Phase-A vendored model deck text (fetched from
   * `/blocks/<slug>/models/<corner>.spice`). Spliced in verbatim — this is the
   * device-model source of record, NOT the engine's bundled blobs.
   */
  modelText: string;
  /** Testbench template. Defaults to {@link INVERTER_TESTBENCH_TEMPLATE}. */
  testbenchTemplate?: string;
}

/** Trim trailing whitespace from a block so joined sections stay tidy. */
function rstrip(text: string): string {
  return text.replace(/\s+$/, "");
}

/**
 * Assemble a complete, self-contained SPICE deck for the playground engine:
 * title comment, the spliced-in vendored model deck, the stimulus-substituted
 * testbench, and the `.temp`/`.tran`/`.end` analysis control. The returned
 * string is ready to pass to `Simulation.setNetList()`.
 */
export function assembleNetlist(options: AssembleNetlistOptions): string {
  const { cell, corner, params, modelText } = options;
  const template = options.testbenchTemplate ?? INVERTER_TESTBENCH_TEMPLATE;
  const vars: Record<string, string> = { ...stimulusVars(params), cell, corner };
  const testbench = renderTemplate(template, vars);

  return (
    [
      `* klt playground deck: ${cell} @ ${corner}`,
      "* Device models: repo-vendored, checksummed phase-A deck (not the engine's bundled models).",
      "",
      "* ---- vendored model deck (phase A) ----",
      rstrip(modelText),
      "",
      "* ---- testbench ----",
      rstrip(testbench),
      "",
      "* ---- analysis ----",
      `.temp ${vars.temperatureC}`,
      `.tran ${vars.stepS} ${vars.stopTimeS}`,
      ".end",
    ].join("\n") + "\n"
  );
}
