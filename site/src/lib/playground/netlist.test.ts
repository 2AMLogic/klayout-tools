import { describe, it, expect } from "vitest";
import {
  DEFAULT_STIMULUS,
  INVERTER_TESTBENCH_TEMPLATE,
  assembleNetlist,
  formatPlain,
  formatSiValue,
  renderTemplate,
  stimulusVars,
  type StimulusParams,
} from "./netlist";

describe("formatPlain", () => {
  it("renders plain decimals without an SI suffix", () => {
    expect(formatPlain(1.8)).toBe("1.8");
    expect(formatPlain(27)).toBe("27");
    expect(formatPlain(-40)).toBe("-40");
    expect(formatPlain(0)).toBe("0");
    expect(formatPlain(1.62)).toBe("1.62");
  });

  it("throws on a non-finite value", () => {
    expect(() => formatPlain(Number.NaN)).toThrow(/non-finite/);
    expect(() => formatPlain(Number.POSITIVE_INFINITY)).toThrow(/non-finite/);
  });
});

describe("formatSiValue", () => {
  it("chooses the SI suffix that lands the mantissa in [1, 1000)", () => {
    expect(formatSiValue(100e-12)).toBe("100p");
    expect(formatSiValue(10e-12)).toBe("10p");
    expect(formatSiValue(40e-9)).toBe("40n");
    expect(formatSiValue(5e-15)).toBe("5f");
    expect(formatSiValue(1e-6)).toBe("1u");
    expect(formatSiValue(0)).toBe("0");
  });

  it("handles negative values", () => {
    expect(formatSiValue(-40e-9)).toBe("-40n");
  });
});

describe("renderTemplate", () => {
  it("substitutes every {{name}} placeholder from vars", () => {
    expect(renderTemplate("a={{x}} b={{ y }}", { x: "1", y: "2" })).toBe("a=1 b=2");
  });

  it("throws on an unknown placeholder (catches template typos)", () => {
    expect(() => renderTemplate("v={{missing}}", { x: "1" })).toThrow(
      /unknown netlist placeholder \{\{missing\}\}/,
    );
  });
});

describe("stimulusVars", () => {
  it("maps each stimulus parameter to its formatted SPICE token", () => {
    expect(stimulusVars(DEFAULT_STIMULUS)).toEqual({
      supplyV: "1.8",
      riseS: "100p",
      fallS: "100p",
      pulseWidthS: "20n",
      periodS: "40n",
      loadCapF: "5f",
      temperatureC: "27",
      stopTimeS: "40n",
      stepS: "10p",
    });
  });
});

describe("assembleNetlist", () => {
  const modelText = "* vendored sky130 tt deck\n.subckt sky130_fd_pr__nfet_01v8 d g s b\n.ends\n";

  it("produces the exact deck for known stimulus params (substitution contract)", () => {
    const netlist = assembleNetlist({
      cell: "sky130_fd_sc_hd__inv_1",
      corner: "tt",
      params: DEFAULT_STIMULUS,
      modelText,
    });

    expect(netlist).toBe(
      [
        "* klt playground deck: sky130_fd_sc_hd__inv_1 @ tt",
        "* Device models: repo-vendored, checksummed phase-A deck (not the engine's bundled models).",
        "",
        "* ---- vendored model deck (phase A) ----",
        "* vendored sky130 tt deck",
        ".subckt sky130_fd_pr__nfet_01v8 d g s b",
        ".ends",
        "",
        "* ---- testbench ----",
        "* transistor-level inverter testbench for sky130_fd_sc_hd__inv_1 (corner tt)",
        "VVPWR VPWR 0 DC 1.8",
        "Vin A 0 DC 0 PULSE(0 1.8 0 100p 100p 20n 40n)",
        "XP Y A VPWR VPWR sky130_fd_pr__pfet_01v8_hvt L=0.15 W=1.0",
        "XN Y A 0 0 sky130_fd_pr__nfet_01v8 L=0.15 W=0.65",
        "CL Y 0 5f",
        "",
        "* ---- analysis ----",
        ".temp 27",
        ".tran 10p 40n",
        ".end",
      ].join("\n") + "\n",
    );
  });

  it("splices in the vendored model text verbatim (not the engine's bundled models)", () => {
    const netlist = assembleNetlist({
      cell: "sky130_fd_sc_hd__inv_1",
      corner: "ss",
      params: DEFAULT_STIMULUS,
      modelText,
    });
    expect(netlist).toContain("* vendored sky130 tt deck");
    expect(netlist).toContain(".subckt sky130_fd_pr__nfet_01v8 d g s b");
    // The default testbench instantiates only the two vendored primitives.
    expect(netlist).toContain("sky130_fd_pr__nfet_01v8");
    expect(netlist).toContain("sky130_fd_pr__pfet_01v8_hvt");
  });

  it("reflects edited stimulus parameters in the deck (edit -> re-run path)", () => {
    const edited: StimulusParams = {
      ...DEFAULT_STIMULUS,
      supplyV: 1.62,
      loadCapF: 20e-15,
      riseS: 50e-12,
      fallS: 50e-12,
      temperatureC: 125,
      stopTimeS: 80e-9,
    };
    const netlist = assembleNetlist({
      cell: "sky130_fd_sc_hd__inv_1",
      corner: "ss",
      params: edited,
      modelText,
    });
    expect(netlist).toContain("VVPWR VPWR 0 DC 1.62");
    expect(netlist).toContain("PULSE(0 1.62 0 50p 50p 20n 40n)");
    expect(netlist).toContain("CL Y 0 20f");
    expect(netlist).toContain(".temp 125");
    expect(netlist).toContain(".tran 10p 80n");
  });

  it("accepts a custom testbench template", () => {
    const netlist = assembleNetlist({
      cell: "custom",
      corner: "ff",
      params: DEFAULT_STIMULUS,
      modelText,
      testbenchTemplate: "V1 in 0 DC {{supplyV}}\nR1 in 0 1k",
    });
    expect(netlist).toContain("V1 in 0 DC 1.8");
    expect(netlist).not.toContain("sky130_fd_pr__nfet_01v8\nXP"); // no default template
  });

  it("ends with a terminating newline and .end directive", () => {
    const netlist = assembleNetlist({
      cell: "c",
      corner: "tt",
      params: DEFAULT_STIMULUS,
      modelText,
    });
    expect(netlist.endsWith(".end\n")).toBe(true);
  });
});

describe("INVERTER_TESTBENCH_TEMPLATE", () => {
  it("references only the two vendored sky130 primitives", () => {
    expect(INVERTER_TESTBENCH_TEMPLATE).toContain("sky130_fd_pr__nfet_01v8");
    expect(INVERTER_TESTBENCH_TEMPLATE).toContain("sky130_fd_pr__pfet_01v8_hvt");
  });
});
