/**
 * Client-side SPICE playground engine wrapper (Epic #90 phase B, issue #150).
 *
 * Public surface:
 *   - `netlist.ts` — pure stimulus-parameter -> netlist assembly (no engine).
 *   - `engine.ts`  — lazy-load / cache / run the `eecircuit-engine` WASM core.
 *
 * Phase C (#151) wires these into the block detail page's playground UI.
 */

export {
  type StimulusParams,
  type AssembleNetlistOptions,
  DEFAULT_STIMULUS,
  INVERTER_TESTBENCH_TEMPLATE,
  assembleNetlist,
  renderTemplate,
  stimulusVars,
  formatPlain,
  formatSiValue,
} from "./netlist";

export {
  type RunPlaygroundOptions,
  type RunPlaygroundResult,
  type PerfSample,
  EECIRCUIT_ENGINE_VERSION,
  EMBEDDED_NGSPICE_VERSION,
  EMBEDDED_NGSPICE_BUILD,
  loadSimulation,
  resetPlaygroundEngine,
  fetchModelDeck,
  extractNgspiceVersion,
  resultToWaveform,
  runPlayground,
  perfProbe,
} from "./engine";
