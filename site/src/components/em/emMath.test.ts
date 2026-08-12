import { describe, expect, it } from "vitest";
import { nearestFrameIndex, s11ToWaveformData } from "./emMath";
import type { EmFrame, EmSParameterPoint } from "./types";

describe("nearestFrameIndex", () => {
  it("resolves to the only frame for a single-frame export regardless of target frequency", () => {
    const frames: EmFrame[] = [{ label: "2.275 GHz", frequency_hz: 2_274_530_000, scalar: [] }];
    expect(nearestFrameIndex(frames, 2_000_000_000)).toBe(0);
    expect(nearestFrameIndex(frames, 3_000_000_000)).toBe(0);
  });

  it("picks the frame whose frequency_hz is closest to the target", () => {
    const frames: EmFrame[] = [
      { label: "2.0 GHz", frequency_hz: 2_000_000_000, scalar: [] },
      { label: "2.5 GHz", frequency_hz: 2_500_000_000, scalar: [] },
      { label: "3.0 GHz", frequency_hz: 3_000_000_000, scalar: [] },
    ];
    expect(nearestFrameIndex(frames, 2_100_000_000)).toBe(0);
    expect(nearestFrameIndex(frames, 2_400_000_000)).toBe(1);
    expect(nearestFrameIndex(frames, 2_900_000_000)).toBe(2);
  });

  it("skips frames with a null frequency_hz", () => {
    const frames: EmFrame[] = [
      { label: "no freq", frequency_hz: null, scalar: [] },
      { label: "2.5 GHz", frequency_hz: 2_500_000_000, scalar: [] },
    ];
    expect(nearestFrameIndex(frames, 2_500_000_000)).toBe(1);
  });

  it("returns 0 for an empty frames array", () => {
    expect(nearestFrameIndex([], 2_500_000_000)).toBe(0);
  });
});

describe("s11ToWaveformData", () => {
  it("maps frequency_hz/s11_db points into WaveformData rows", () => {
    const points: EmSParameterPoint[] = [
      { frequency_hz: 2_000_000_000, s11_db: -0.39 },
      { frequency_hz: 2_100_000_000, s11_db: -0.7 },
    ];
    const wf = s11ToWaveformData("patch_antenna", points);

    expect(wf.plotname).toBe("S11 (patch_antenna)");
    expect(wf.variables).toEqual([
      { index: 0, name: "frequency_hz", type: "frequency" },
      { index: 1, name: "s11_db", type: "gain" },
    ]);
    expect(wf.points).toEqual([
      [2_000_000_000, -0.39],
      [2_100_000_000, -0.7],
    ]);
  });

  it("produces an empty points array for an empty sweep", () => {
    const wf = s11ToWaveformData("patch_antenna", []);
    expect(wf.points).toEqual([]);
  });
});
