import { describe, expect, it } from "vitest";
import { PLAYGROUND_SLUGS, isPlaygroundEligible } from "./types";

describe("isPlaygroundEligible", () => {
  it("is true for every staged sky130 gallery cell", () => {
    for (const slug of PLAYGROUND_SLUGS) {
      expect(isPlaygroundEligible(slug)).toBe(true);
    }
  });

  it("is false for a gf180mcu cell (out of scope per the spike's boundary)", () => {
    expect(isPlaygroundEligible("gf180mcu_fd_sc_mcu9t5v0__clkinv_1")).toBe(false);
  });

  it("is false for a canary block with no vendored playground model deck", () => {
    expect(isPlaygroundEligible("sky130-bandgap")).toBe(false);
  });

  it("is false for an unknown slug", () => {
    expect(isPlaygroundEligible("not-a-real-block")).toBe(false);
  });
});
