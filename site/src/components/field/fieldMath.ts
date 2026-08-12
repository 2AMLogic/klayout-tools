/**
 * Pure helper functions backing `FieldViewer` (mesh bounds, field
 * normalization, the viridis colormap, minimal 4x4 matrix math for the
 * orbit camera, cell triangulation). Split out from the component so
 * they're unit-testable without a DOM or a WebGL context — mirroring
 * `waveform/waveformMath.ts`'s separation of pure math from rendering.
 */
import type { FieldFrame } from "./types";

export type Vec3 = [number, number, number];
/** Column-major 4x4 matrix, the layout `WebGLRenderingContext.uniformMatrix4fv`
 *  expects with `transpose = false`. */
export type Mat4 = Float32Array;

/** Clamp `value` to `[lo, hi]`. */
export function clamp(value: number, lo: number, hi: number): number {
  return Math.min(hi, Math.max(lo, value));
}

export interface MeshBounds {
  center: Vec3;
  radius: number;
}

/** Bounding sphere (center + radius) of `vertices`, used to frame the orbit
 *  camera automatically regardless of the mesh's own coordinate scale.
 *  Empty input returns a unit sphere at the origin rather than a
 *  degenerate/NaN result. */
export function meshBounds(vertices: number[][]): MeshBounds {
  if (vertices.length === 0) return { center: [0, 0, 0], radius: 1 };
  let minX = Infinity;
  let minY = Infinity;
  let minZ = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;
  let maxZ = -Infinity;
  for (const v of vertices) {
    const x = v[0] ?? 0;
    const y = v[1] ?? 0;
    const z = v[2] ?? 0;
    if (x < minX) minX = x;
    if (x > maxX) maxX = x;
    if (y < minY) minY = y;
    if (y > maxY) maxY = y;
    if (z < minZ) minZ = z;
    if (z > maxZ) maxZ = z;
  }
  const center: Vec3 = [(minX + maxX) / 2, (minY + maxY) / 2, (minZ + maxZ) / 2];
  let radius = 0;
  for (const v of vertices) {
    const x = v[0] ?? 0;
    const y = v[1] ?? 0;
    const z = v[2] ?? 0;
    const d = Math.hypot(x - center[0], y - center[1], z - center[2]);
    if (d > radius) radius = d;
  }
  return { center, radius: radius > 0 ? radius : 1 };
}

/** The `[lo, hi]` range of `values`, ignoring non-finite entries. Empty (or
 *  all-non-finite) input falls back to `[0, 1]`; a degenerate single-value
 *  range widens by +/-1 so a downstream normalize() never divides by zero. */
export function fieldDomain(values: number[]): [number, number] {
  let lo = Infinity;
  let hi = -Infinity;
  for (const v of values) {
    if (!Number.isFinite(v)) continue;
    if (v < lo) lo = v;
    if (v > hi) hi = v;
  }
  if (!Number.isFinite(lo) || !Number.isFinite(hi)) return [0, 1];
  if (lo === hi) return [lo - 1, hi + 1];
  return [lo, hi];
}

/** Map `value` into `[0, 1]` given `domain`, clamped at both ends.
 *  Non-finite input normalizes to `0` rather than propagating `NaN`. */
export function normalize(value: number, domain: [number, number]): number {
  if (!Number.isFinite(value)) return 0;
  const [lo, hi] = domain;
  const span = hi - lo || 1;
  return clamp((value - lo) / span, 0, 1);
}

/** Euclidean magnitude of a (possibly short) vector, treating missing
 *  components as 0. */
export function vectorMagnitude(v: number[]): number {
  return Math.hypot(v[0] ?? 0, v[1] ?? 0, v[2] ?? 0);
}

/** A 5-stop approximation of the viridis colormap (perceptually uniform,
 *  colorblind-friendly — the same reasoning most scientific-viz tools pick
 *  it as their default sequential map). Values are 0-255 per channel. */
const VIRIDIS_STOPS: Array<{ t: number; rgb: Vec3 }> = [
  { t: 0, rgb: [68, 1, 84] },
  { t: 0.25, rgb: [59, 82, 139] },
  { t: 0.5, rgb: [33, 145, 140] },
  { t: 0.75, rgb: [94, 201, 98] },
  { t: 1, rgb: [253, 231, 37] },
];

/** Map `t` (clamped to `[0, 1]`) to an RGB color (0-255 per channel) along
 *  the viridis colormap. */
export function colormap(t: number): Vec3 {
  const clamped = clamp(Number.isFinite(t) ? t : 0, 0, 1);
  for (let i = 0; i < VIRIDIS_STOPS.length - 1; i++) {
    const a = VIRIDIS_STOPS[i];
    const b = VIRIDIS_STOPS[i + 1];
    if (!a || !b) continue;
    if (clamped >= a.t && clamped <= b.t) {
      const span = b.t - a.t || 1;
      const localT = (clamped - a.t) / span;
      return [
        a.rgb[0] + (b.rgb[0] - a.rgb[0]) * localT,
        a.rgb[1] + (b.rgb[1] - a.rgb[1]) * localT,
        a.rgb[2] + (b.rgb[2] - a.rgb[2]) * localT,
      ];
    }
  }
  const last = VIRIDIS_STOPS[VIRIDIS_STOPS.length - 1];
  return last ? last.rgb : [253, 231, 37];
}

/** `colormap(t)` as a CSS `rgb()` string, for the legend gradient / any
 *  DOM-side color swatch. */
export function colormapCss(t: number): string {
  const [r, g, b] = colormap(t);
  return `rgb(${Math.round(r)}, ${Math.round(g)}, ${Math.round(b)})`;
}

/** A CSS `linear-gradient` spanning the full colormap, for the legend bar. */
export function legendGradientCss(stopCount = 8): string {
  const stops: string[] = [];
  for (let i = 0; i <= stopCount; i++) {
    const t = i / stopCount;
    stops.push(`${colormapCss(t)} ${(t * 100).toFixed(0)}%`);
  }
  return `linear-gradient(to right, ${stops.join(", ")})`;
}

/** The per-vertex scalar array a frame colormaps by: `frame.scalar`
 *  directly, or each `frame.vector` entry's magnitude when `scalar` is
 *  omitted. `undefined` when the frame carries neither. */
export function frameScalars(frame: FieldFrame): number[] | undefined {
  return frame.scalar ?? frame.vector?.map(vectorMagnitude);
}

/** `fieldDomain()` of `frameScalars(frame)`, or `[0, 1]` when the frame has
 *  no field data at all. */
export function frameDomain(frame: FieldFrame): [number, number] {
  const scalars = frameScalars(frame);
  return scalars && scalars.length > 0 ? fieldDomain(scalars) : [0, 1];
}

/** Per-vertex RGB colors (0..1 per channel — WebGL vertex-attribute range)
 *  for `frame`, via the viridis colormap over `frameScalars(frame)`.
 *  Vertices with no corresponding sample (a shorter `scalar`/`vector` array
 *  than `vertexCount`) color mid-scale (0.5) rather than throwing. */
export function frameColors(frame: FieldFrame, vertexCount: number): Float32Array {
  const scalars = frameScalars(frame);
  const domain = frameDomain(frame);
  const out = new Float32Array(vertexCount * 3);
  for (let i = 0; i < vertexCount; i++) {
    const raw = scalars?.[i];
    const t = raw === undefined ? 0.5 : normalize(raw, domain);
    const [r, g, b] = colormap(t);
    out[i * 3] = r / 255;
    out[i * 3 + 1] = g / 255;
    out[i * 3 + 2] = b / 255;
  }
  return out;
}

/** Fan-triangulate `cells` (each an ordered polygon's vertex indices,
 *  length >= 3) into a flat `[i0, i1, i2, ...]` triangle index list for
 *  `gl.drawElements`. Assumes convex, roughly planar cells (true of the
 *  triangulated/quad BEM/FEM surface meshes this component targets) — a
 *  non-convex cell renders with a visual artifact but never throws. */
export function triangulateCells(cells: number[][]): number[] {
  const indices: number[] = [];
  for (const cell of cells) {
    if (cell.length < 3) continue;
    const anchor = cell[0];
    if (anchor === undefined) continue;
    for (let i = 1; i < cell.length - 1; i++) {
      const b = cell[i];
      const c = cell[i + 1];
      if (b === undefined || c === undefined) continue;
      indices.push(anchor, b, c);
    }
  }
  return indices;
}

/** Format a field magnitude for the legend: fixed notation for a
 *  "reasonable" range, scientific notation outside it (typical EM field
 *  magnitudes span many decades). */
export function formatMagnitude(value: number): string {
  if (!Number.isFinite(value)) return String(value);
  if (value === 0) return "0";
  const abs = Math.abs(value);
  if (abs < 1e-3 || abs >= 1e5) return value.toExponential(2);
  return Number(value.toPrecision(4)).toString();
}

/** A right-handed perspective projection matrix (column-major, matching
 *  `Mat4`), equivalent to the standard `gluPerspective`/glMatrix formula. */
export function perspective(fovYRadians: number, aspect: number, near: number, far: number): Mat4 {
  const f = 1 / Math.tan(fovYRadians / 2);
  const out = new Float32Array(16);
  out[0] = f / (aspect || 1);
  out[5] = f;
  out[11] = -1;
  if (Number.isFinite(far)) {
    const nf = 1 / (near - far);
    out[10] = (far + near) * nf;
    out[14] = 2 * far * near * nf;
  } else {
    out[10] = -1;
    out[14] = -2 * near;
  }
  return out;
}

/** A right-handed view matrix looking from `eye` toward `center`, with
 *  `up` defining the vertical, matching the standard `gluLookAt` formula. */
export function lookAt(eye: Vec3, center: Vec3, up: Vec3): Mat4 {
  let z0 = eye[0] - center[0];
  let z1 = eye[1] - center[1];
  let z2 = eye[2] - center[2];
  let len = Math.hypot(z0, z1, z2) || 1;
  z0 /= len;
  z1 /= len;
  z2 /= len;

  let x0 = up[1] * z2 - up[2] * z1;
  let x1 = up[2] * z0 - up[0] * z2;
  let x2 = up[0] * z1 - up[1] * z0;
  len = Math.hypot(x0, x1, x2) || 1;
  x0 /= len;
  x1 /= len;
  x2 /= len;

  const y0 = z1 * x2 - z2 * x1;
  const y1 = z2 * x0 - z0 * x2;
  const y2 = z0 * x1 - z1 * x0;

  const out = new Float32Array(16);
  out[0] = x0;
  out[1] = y0;
  out[2] = z0;
  out[3] = 0;
  out[4] = x1;
  out[5] = y1;
  out[6] = z1;
  out[7] = 0;
  out[8] = x2;
  out[9] = y2;
  out[10] = z2;
  out[11] = 0;
  out[12] = -(x0 * eye[0] + x1 * eye[1] + x2 * eye[2]);
  out[13] = -(y0 * eye[0] + y1 * eye[1] + y2 * eye[2]);
  out[14] = -(z0 * eye[0] + z1 * eye[1] + z2 * eye[2]);
  out[15] = 1;
  return out;
}

/** `a * b` for two column-major 4x4 matrices — applying the result to a
 *  vector `v` is equivalent to `a * (b * v)`. */
export function multiplyMat4(a: Mat4, b: Mat4): Mat4 {
  const out = new Float32Array(16);
  for (let col = 0; col < 4; col++) {
    for (let row = 0; row < 4; row++) {
      let sum = 0;
      for (let k = 0; k < 4; k++) {
        sum += (a[k * 4 + row] ?? 0) * (b[col * 4 + k] ?? 0);
      }
      out[col * 4 + row] = sum;
    }
  }
  return out;
}

/** Identity 4x4 matrix. */
export function identityMat4(): Mat4 {
  const out = new Float32Array(16);
  out[0] = 1;
  out[5] = 1;
  out[10] = 1;
  out[15] = 1;
  return out;
}

/** Orbit-camera eye position at `distance` from `center`, at `yaw`/`pitch`
 *  radians (spherical coordinates around `center`). `pitch` is clamped away
 *  from the poles to avoid a gimbal-flip singularity in `lookAt`'s cross
 *  product. */
export function orbitEye(center: Vec3, distance: number, yaw: number, pitch: number): Vec3 {
  const maxPitch = Math.PI / 2 - 0.01;
  const p = clamp(pitch, -maxPitch, maxPitch);
  const x = center[0] + distance * Math.cos(p) * Math.sin(yaw);
  const y = center[1] + distance * Math.sin(p);
  const z = center[2] + distance * Math.cos(p) * Math.cos(yaw);
  return [x, y, z];
}
