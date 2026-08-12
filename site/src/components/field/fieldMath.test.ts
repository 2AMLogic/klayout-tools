import { describe, expect, it } from "vitest";
import {
  clamp,
  colormap,
  colormapCss,
  fieldDomain,
  formatMagnitude,
  frameColors,
  frameDomain,
  frameScalars,
  identityMat4,
  legendGradientCss,
  lookAt,
  meshBounds,
  multiplyMat4,
  normalize,
  orbitEye,
  perspective,
  triangulateCells,
  vectorMagnitude,
} from "./fieldMath";
import type { FieldFrame } from "./types";

describe("clamp", () => {
  it("clamps below and above the range", () => {
    expect(clamp(-1, 0, 1)).toBe(0);
    expect(clamp(2, 0, 1)).toBe(1);
    expect(clamp(0.5, 0, 1)).toBe(0.5);
  });
});

describe("meshBounds", () => {
  it("returns a unit sphere at the origin for an empty mesh", () => {
    expect(meshBounds([])).toEqual({ center: [0, 0, 0], radius: 1 });
  });

  it("computes the center and bounding radius of a simple mesh", () => {
    const bounds = meshBounds([
      [-1, 0, 0],
      [1, 0, 0],
      [0, 1, 0],
      [0, -1, 0],
    ]);
    expect(bounds.center).toEqual([0, 0, 0]);
    expect(bounds.radius).toBeCloseTo(1, 10);
  });

  it("never returns a zero radius, even for a single-vertex mesh", () => {
    const bounds = meshBounds([[5, 5, 5]]);
    expect(bounds.center).toEqual([5, 5, 5]);
    expect(bounds.radius).toBe(1);
  });
});

describe("fieldDomain", () => {
  it("returns [0, 1] for empty input", () => {
    expect(fieldDomain([])).toEqual([0, 1]);
  });

  it("ignores non-finite entries", () => {
    expect(fieldDomain([1, Number.NaN, 3, Number.POSITIVE_INFINITY])).toEqual([1, 3]);
  });

  it("widens a degenerate single-value range", () => {
    expect(fieldDomain([4, 4, 4])).toEqual([3, 5]);
  });

  it("returns [0, 1] when every value is non-finite", () => {
    expect(fieldDomain([Number.NaN, Number.POSITIVE_INFINITY])).toEqual([0, 1]);
  });

  it("returns the min/max of a normal range", () => {
    expect(fieldDomain([3, -2, 7, 0])).toEqual([-2, 7]);
  });
});

describe("normalize", () => {
  it("maps a value to [0, 1] within the domain", () => {
    expect(normalize(5, [0, 10])).toBe(0.5);
    expect(normalize(0, [0, 10])).toBe(0);
    expect(normalize(10, [0, 10])).toBe(1);
  });

  it("clamps outside the domain", () => {
    expect(normalize(-5, [0, 10])).toBe(0);
    expect(normalize(15, [0, 10])).toBe(1);
  });

  it("returns 0 for non-finite input instead of NaN", () => {
    expect(normalize(Number.NaN, [0, 10])).toBe(0);
  });
});

describe("vectorMagnitude", () => {
  it("computes the Euclidean magnitude", () => {
    expect(vectorMagnitude([3, 4, 0])).toBe(5);
  });

  it("treats missing components as 0", () => {
    expect(vectorMagnitude([3])).toBe(3);
    expect(vectorMagnitude([])).toBe(0);
  });
});

describe("colormap", () => {
  it("returns the first stop's color at t=0", () => {
    expect(colormap(0)).toEqual([68, 1, 84]);
  });

  it("returns the last stop's color at t=1", () => {
    expect(colormap(1)).toEqual([253, 231, 37]);
  });

  it("clamps out-of-range t", () => {
    expect(colormap(-1)).toEqual(colormap(0));
    expect(colormap(2)).toEqual(colormap(1));
  });

  it("treats non-finite t as 0", () => {
    expect(colormap(Number.NaN)).toEqual(colormap(0));
  });

  it("interpolates monotonically between stops", () => {
    const low = colormap(0.1);
    const mid = colormap(0.2);
    const high = colormap(0.24);
    // Approaching the second stop, red should be decreasing toward it.
    expect(low[0]).toBeGreaterThan(mid[0]);
    expect(mid[0]).toBeGreaterThan(high[0]);
  });
});

describe("colormapCss / legendGradientCss", () => {
  it("formats an rgb() string", () => {
    expect(colormapCss(0)).toBe("rgb(68, 1, 84)");
  });

  it("builds a linear-gradient spanning the requested stop count", () => {
    const css = legendGradientCss(2);
    expect(css).toBe("linear-gradient(to right, rgb(68, 1, 84) 0%, rgb(33, 145, 140) 50%, rgb(253, 231, 37) 100%)");
  });
});

describe("frameScalars / frameDomain / frameColors", () => {
  it("uses scalar directly when present", () => {
    const frame: FieldFrame = { scalar: [1, 2, 3] };
    expect(frameScalars(frame)).toEqual([1, 2, 3]);
  });

  it("falls back to vector magnitude when scalar is absent", () => {
    const frame: FieldFrame = {
      vector: [
        [3, 4, 0],
        [0, 0, 5],
      ],
    };
    expect(frameScalars(frame)).toEqual([5, 5]);
  });

  it("returns undefined when the frame has neither", () => {
    expect(frameScalars({})).toBeUndefined();
  });

  it("frameDomain falls back to [0, 1] with no field data", () => {
    expect(frameDomain({})).toEqual([0, 1]);
  });

  it("frameColors colors every vertex, mid-scale for missing samples", () => {
    const colors = frameColors({ scalar: [0, 1] }, 3);
    expect(colors).toHaveLength(9);
    // vertex 0 -> t=0 -> first stop, normalized to 0..1
    expect(colors[0]).toBeCloseTo(68 / 255, 5);
    // vertex 1 -> t=1 -> last stop
    expect(colors[3]).toBeCloseTo(253 / 255, 5);
    // vertex 2 has no sample -> mid-scale (t=0.5) -> middle stop
    expect(colors[6]).toBeCloseTo(33 / 255, 5);
  });
});

describe("triangulateCells", () => {
  it("passes through a triangle unchanged", () => {
    expect(triangulateCells([[0, 1, 2]])).toEqual([0, 1, 2]);
  });

  it("fan-triangulates a quad", () => {
    expect(triangulateCells([[0, 1, 2, 3]])).toEqual([0, 1, 2, 0, 2, 3]);
  });

  it("skips degenerate cells with fewer than 3 vertices", () => {
    expect(triangulateCells([[0, 1], [2, 3, 4]])).toEqual([2, 3, 4]);
  });

  it("handles multiple cells", () => {
    expect(
      triangulateCells([
        [0, 1, 2],
        [3, 4, 5],
      ]),
    ).toEqual([0, 1, 2, 3, 4, 5]);
  });
});

describe("formatMagnitude", () => {
  it("formats a normal-range value with fixed notation", () => {
    expect(formatMagnitude(1.2345)).toBe("1.234");
    expect(formatMagnitude(0)).toBe("0");
  });

  it("uses scientific notation for very small or large magnitudes", () => {
    expect(formatMagnitude(1.5e-8)).toBe("1.50e-8");
    expect(formatMagnitude(2.5e8)).toBe("2.50e+8");
  });

  it("passes through non-finite values as strings", () => {
    expect(formatMagnitude(Number.NaN)).toBe("NaN");
  });
});

describe("matrix helpers", () => {
  it("identityMat4 leaves a matrix unchanged under multiplication", () => {
    const m = perspective(Math.PI / 4, 1.5, 0.1, 100);
    const identity = identityMat4();
    const result = multiplyMat4(identity, m);
    for (let i = 0; i < 16; i++) {
      expect(result[i]).toBeCloseTo(m[i] ?? 0, 5);
    }
  });

  it("lookAt from the +Z axis toward the origin has no rotation about Y", () => {
    const view = lookAt([0, 0, 5], [0, 0, 0], [0, 1, 0]);
    // Camera looks down -Z with no roll: right = +X, up = +Y, back = +Z.
    expect(view[0]).toBeCloseTo(1, 5); // right.x
    expect(view[5]).toBeCloseTo(1, 5); // up.y
    expect(view[10]).toBeCloseTo(1, 5); // back.z
  });

  it("perspective produces a matrix with the expected sign convention", () => {
    const m = perspective(Math.PI / 2, 1, 1, 100);
    expect(m[11]).toBe(-1);
    expect(m[15]).toBe(0);
  });
});

describe("orbitEye", () => {
  it("places the eye at the requested distance from center", () => {
    const eye = orbitEye([0, 0, 0], 10, 0, 0);
    const dist = Math.hypot(eye[0], eye[1], eye[2]);
    expect(dist).toBeCloseTo(10, 5);
  });

  it("at yaw=0, pitch=0 the eye sits on the +Z axis relative to center", () => {
    const eye = orbitEye([1, 2, 3], 5, 0, 0);
    expect(eye[0]).toBeCloseTo(1, 5);
    expect(eye[1]).toBeCloseTo(2, 5);
    expect(eye[2]).toBeCloseTo(8, 5);
  });

  it("clamps pitch away from the poles", () => {
    const eyeAtPole = orbitEye([0, 0, 0], 10, 0, Math.PI / 2);
    const eyeNearPole = orbitEye([0, 0, 0], 10, 0, Math.PI / 2 - 0.01);
    expect(eyeAtPole[1]).toBeCloseTo(eyeNearPole[1], 2);
  });
});
