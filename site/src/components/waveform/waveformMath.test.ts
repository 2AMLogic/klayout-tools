import { describe, expect, it } from "vitest";
import {
  clampDomain,
  formatEngineering,
  fullDomain,
  nearestIndex,
  nearestSample,
  zoomDomain,
} from "./waveformMath";
import type { WaveformData } from "./types";

const wf = (points: number[][]): WaveformData => ({
  plotname: "Transient Analysis",
  variables: [
    { index: 0, name: "time", type: "time" },
    { index: 1, name: "v(out)", type: "voltage" },
  ],
  points,
});

describe("fullDomain", () => {
  it("returns null for waveforms with no points", () => {
    expect(fullDomain([wf([])])).toBeNull();
    expect(fullDomain([])).toBeNull();
  });

  it("returns [min, max] of the sweep variable across waveforms", () => {
    const a = wf([
      [0, 0],
      [1, 1],
      [2, 2],
    ]);
    const b = wf([
      [-1, 0],
      [3, 1],
    ]);
    expect(fullDomain([a, b])).toEqual([-1, 3]);
  });

  it("widens a single-point domain by 1 so it is non-degenerate", () => {
    expect(fullDomain([wf([[5, 1]])])).toEqual([5, 6]);
  });
});

describe("clampDomain", () => {
  it("leaves a domain already inside bounds unchanged", () => {
    expect(clampDomain([2, 4], [0, 10])).toEqual([2, 4]);
  });

  it("shifts a domain back inside bounds, preserving width", () => {
    expect(clampDomain([-2, 2], [0, 10])).toEqual([0, 4]);
    expect(clampDomain([8, 12], [0, 10])).toEqual([6, 10]);
  });

  it("clamps a too-wide domain to the full bounds", () => {
    expect(clampDomain([-5, 15], [0, 10])).toEqual([0, 10]);
  });
});

describe("zoomDomain", () => {
  const bounds: [number, number] = [0, 100];

  it("zooms in (factor < 1) around the center by default", () => {
    const result = zoomDomain([0, 100], 0.5, bounds);
    expect(result[1] - result[0]).toBeCloseTo(50);
    expect(result[0]).toBeCloseTo(25);
    expect(result[1]).toBeCloseTo(75);
  });

  it("zooms out (factor > 1), clamped to bounds", () => {
    const result = zoomDomain([40, 60], 20, bounds);
    expect(result).toEqual([0, 100]);
  });

  it("never zooms in narrower than the bounds-derived floor", () => {
    const tiny = zoomDomain([49, 51], 1e-9, bounds);
    expect(tiny[1] - tiny[0]).toBeGreaterThan(0);
  });

  it("zooms around an off-center focal point", () => {
    const result = zoomDomain([0, 100], 0.5, bounds, 0.1);
    // Focal value is 10; the point at x=10 should remain at the same
    // fractional position (0.1) within the new, narrower domain.
    const focalValue = 10;
    const frac = (focalValue - result[0]) / (result[1] - result[0]);
    expect(frac).toBeCloseTo(0.1, 5);
  });
});

describe("nearestIndex / nearestSample", () => {
  const points = [
    [0, 0],
    [1, 10],
    [2, 20],
    [5, 50],
  ];

  it("returns -1 for an empty points array", () => {
    expect(nearestIndex([], 1)).toBe(-1);
  });

  it("finds an exact match", () => {
    expect(nearestIndex(points, 2)).toBe(2);
  });

  it("finds the nearer of two straddling points", () => {
    expect(nearestIndex(points, 1.6)).toBe(2); // closer to 2 than 1
    expect(nearestIndex(points, 1.4)).toBe(1); // closer to 1 than 2
  });

  it("clamps to the first/last index outside the range", () => {
    expect(nearestIndex(points, -10)).toBe(0);
    expect(nearestIndex(points, 100)).toBe(3);
  });

  it("nearestSample returns the matching row", () => {
    expect(nearestSample(wf(points), 2)).toEqual([2, 20]);
  });

  it("nearestSample returns null for an empty waveform", () => {
    expect(nearestSample(wf([]), 2)).toBeNull();
  });
});

describe("formatEngineering", () => {
  it("formats sub-nanosecond values with a pico prefix", () => {
    expect(formatEngineering(1.2e-10, "s")).toBe("120.00 ps");
  });

  it("formats volts with no prefix at unit scale", () => {
    expect(formatEngineering(1.8, "V")).toBe("1.80 V");
  });

  it("formats zero without a prefix", () => {
    expect(formatEngineering(0, "V")).toBe("0 V");
  });

  it("formats without a unit when none is given", () => {
    expect(formatEngineering(2.5e-9)).toBe("2.50 n");
  });
});
