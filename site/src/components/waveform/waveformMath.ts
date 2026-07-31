/**
 * Pure helper functions backing `WaveformViewer` (zoom-domain math, nearest-
 * sample lookup, engineering-notation formatting). Split out from the
 * component so they're unit-testable without a DOM.
 */
import type { WaveformData } from "./types";

export type Domain = [number, number];

/** The full sweep-variable (`variables[0]`, e.g. `time`) range across one or
 *  more waveforms, or `null` when none have any points. */
export function fullDomain(waveforms: WaveformData[]): Domain | null {
  let lo = Infinity;
  let hi = -Infinity;
  for (const wf of waveforms) {
    for (const row of wf.points) {
      const x = row[0];
      if (x === undefined) continue;
      if (x < lo) lo = x;
      if (x > hi) hi = x;
    }
  }
  if (!Number.isFinite(lo) || !Number.isFinite(hi)) return null;
  if (lo === hi) return [lo, lo + 1];
  return [lo, hi];
}

/** Clamp `domain` inside `bounds`, preserving its width where possible. */
export function clampDomain(domain: Domain, bounds: Domain): Domain {
  const width = domain[1] - domain[0];
  const boundsWidth = bounds[1] - bounds[0];
  const clampedWidth = Math.min(width, boundsWidth);
  let lo = Math.max(bounds[0], Math.min(domain[0], bounds[1] - clampedWidth));
  let hi = lo + clampedWidth;
  if (hi > bounds[1]) {
    hi = bounds[1];
    lo = hi - clampedWidth;
  }
  return [lo, hi];
}

/**
 * Zoom `domain` by `factor` (`< 1` zooms in, `> 1` zooms out) around
 * `focal` (a fraction 0..1 of the current domain width, default center),
 * clamped to `bounds` and never narrower than `minWidth`.
 */
export function zoomDomain(
  domain: Domain,
  factor: number,
  bounds: Domain,
  focal = 0.5,
  minWidth = 0,
): Domain {
  const [lo, hi] = domain;
  const width = hi - lo;
  const focalValue = lo + width * focal;
  const boundsWidth = bounds[1] - bounds[0];
  const floor = minWidth > 0 ? minWidth : boundsWidth > 0 ? boundsWidth / 1e6 : 1e-12;
  const newWidth = Math.max(floor, Math.min(boundsWidth, width * factor));
  const newLo = focalValue - newWidth * focal;
  const newHi = newLo + newWidth;
  return clampDomain([newLo, newHi], bounds);
}

/** Index of the point in `points` whose sweep value (`points[i][0]`) is
 *  closest to `x`. Assumes `points` is sorted ascending by sweep value
 *  (true of every `klt sim` waveform artifact). Returns -1 for empty input. */
export function nearestIndex(points: number[][], x: number): number {
  if (points.length === 0) return -1;
  let loIdx = 0;
  let hiIdx = points.length - 1;
  while (loIdx < hiIdx) {
    const mid = (loIdx + hiIdx) >> 1;
    const midX = points[mid]?.[0] ?? 0;
    if (midX < x) loIdx = mid + 1;
    else hiIdx = mid;
  }
  if (loIdx > 0) {
    const prevX = points[loIdx - 1]?.[0] ?? 0;
    const curX = points[loIdx]?.[0] ?? 0;
    if (Math.abs(prevX - x) <= Math.abs(curX - x)) return loIdx - 1;
  }
  return loIdx;
}

/** The sample row nearest `x`, or `null` for an empty waveform. */
export function nearestSample(waveform: WaveformData, x: number): number[] | null {
  const idx = nearestIndex(waveform.points, x);
  return idx >= 0 ? (waveform.points[idx] ?? null) : null;
}

const SI_PREFIXES: Array<[number, string]> = [
  [1e9, "G"],
  [1e6, "M"],
  [1e3, "k"],
  [1, ""],
  [1e-3, "m"],
  [1e-6, "µ"],
  [1e-9, "n"],
  [1e-12, "p"],
  [1e-15, "f"],
];

/** Format `value` with an SI/engineering-notation prefix, e.g.
 *  `formatEngineering(1.2e-10, "s")` -> `"120.00 ps"`. `0` and non-finite
 *  values are formatted without a prefix. */
export function formatEngineering(value: number, unit = ""): string {
  if (!Number.isFinite(value)) return `${value}${unit ? ` ${unit}` : ""}`;
  if (value === 0) return `0${unit ? ` ${unit}` : ""}`;
  const abs = Math.abs(value);
  for (const [scale, prefix] of SI_PREFIXES) {
    if (abs >= scale) {
      return `${(value / scale).toFixed(2)} ${prefix}${unit}`;
    }
  }
  const [scale, prefix] = SI_PREFIXES[SI_PREFIXES.length - 1] as [number, string];
  return `${(value / scale).toFixed(2)} ${prefix}${unit}`;
}
