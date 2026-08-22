/**
 * Hierarchy flattening for the embedded GDS viewer (issue #943 / #1284).
 *
 * `parseGds()` returns structures that still reference each other through
 * SREF/AREF; a canvas renderer wants one flat list of shapes per
 * `(layer, datatype)` in top-cell coordinates. This module walks the
 * reference tree, composes the affine transforms, and emits that list —
 * bounded by explicit shape/depth caps so a pathological file degrades into
 * a partial view (`truncated: true`) instead of hanging the browser tab.
 *
 * Coordinates stay in database units (integers from the stream); the caller
 * scales by `library.dbuMicrons` for display.
 */
import type { GdsLibrary, GdsStructure } from "./parseGds";
import { findTopStructure } from "./parseGds";

/** Affine transform `x' = a·x + c·y + e`, `y' = b·x + d·y + f`. */
export interface Affine {
  a: number;
  b: number;
  c: number;
  d: number;
  e: number;
  f: number;
}

export const IDENTITY: Affine = { a: 1, b: 0, c: 0, d: 1, e: 0, f: 0 };

/**
 * Builds the transform for a structure reference. GDSII applies, in order:
 * reflection about the x-axis (STRANS bit 15), magnification, rotation
 * (counter-clockwise degrees), then translation.
 */
export function refTransform(
  reflect: boolean,
  mag: number,
  angleDegrees: number,
  dx: number,
  dy: number,
): Affine {
  const radians = (angleDegrees * Math.PI) / 180;
  const cos = Math.cos(radians);
  const sin = Math.sin(radians);
  const m = mag || 1;
  const r = reflect ? -1 : 1;
  return {
    a: m * cos,
    b: m * sin,
    c: -m * r * sin,
    d: m * r * cos,
    e: dx,
    f: dy,
  };
}

/** Returns the transform applying `child` first, then `parent`. */
export function composeAffine(parent: Affine, child: Affine): Affine {
  return {
    a: parent.a * child.a + parent.c * child.b,
    b: parent.b * child.a + parent.d * child.b,
    c: parent.a * child.c + parent.c * child.d,
    d: parent.b * child.c + parent.d * child.d,
    e: parent.a * child.e + parent.c * child.f + parent.e,
    f: parent.b * child.e + parent.d * child.f + parent.f,
  };
}

/** Uniform scale factor of a transform, used to scale PATH widths. */
export function affineScale(t: Affine): number {
  return Math.sqrt(Math.abs(t.a * t.d - t.b * t.c)) || 1;
}

export interface FlatPath {
  /** Flat `[x0, y0, ...]` centerline in top-cell database units. */
  points: number[];
  width: number;
  pathtype: number;
}

export interface FlatText {
  x: number;
  y: number;
  text: string;
}

export interface FlatLayer {
  layer: number;
  datatype: number;
  /** `"<layer>/<datatype>"`, the stable id used for visibility toggles. */
  key: string;
  /** Each entry is a flat `[x0, y0, ...]` ring in top-cell database units. */
  polygons: number[][];
  paths: FlatPath[];
  texts: FlatText[];
  /** Polygons + paths (texts are labels, not geometry). */
  shapeCount: number;
}

export interface Bbox {
  minX: number;
  minY: number;
  maxX: number;
  maxY: number;
}

export interface FlatLayout {
  topName: string;
  /** Microns per database unit, carried through from the library. */
  dbuMicrons: number;
  bbox: Bbox | null;
  /** Sorted by layer then datatype, which is also the draw order. */
  layers: FlatLayer[];
  shapeCount: number;
  /** True when a cap below was hit and the result is a partial view. */
  truncated: boolean;
}

export interface FlattenOptions {
  /** Structure to flatten; defaults to `findTopStructure()`. */
  top?: string;
  /** Hard cap on emitted polygons + paths (default 250 000). */
  maxShapes?: number;
  /** Hard cap on reference nesting depth (default 64). */
  maxDepth?: number;
  /** Hard cap on emitted text labels (default 5 000). */
  maxTexts?: number;
}

const DEFAULT_MAX_SHAPES = 250_000;
const DEFAULT_MAX_DEPTH = 64;
const DEFAULT_MAX_TEXTS = 5_000;

function transformPoints(xy: number[], t: Affine): number[] {
  const out = new Array<number>(xy.length);
  for (let i = 0; i + 1 < xy.length; i += 2) {
    const x = xy[i];
    const y = xy[i + 1];
    out[i] = t.a * x + t.c * y + t.e;
    out[i + 1] = t.b * x + t.d * y + t.f;
  }
  return out;
}

export function flattenGds(library: GdsLibrary, options: FlattenOptions = {}): FlatLayout {
  const maxShapes = options.maxShapes ?? DEFAULT_MAX_SHAPES;
  const maxDepth = options.maxDepth ?? DEFAULT_MAX_DEPTH;
  const maxTexts = options.maxTexts ?? DEFAULT_MAX_TEXTS;

  const topName = options.top ?? findTopStructure(library) ?? "";
  const layers = new Map<string, FlatLayer>();
  let shapeCount = 0;
  let textCount = 0;
  let truncated = false;
  let minX = Infinity;
  let minY = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;

  function layerFor(layer: number, datatype: number): FlatLayer {
    const key = `${layer}/${datatype}`;
    let entry = layers.get(key);
    if (!entry) {
      entry = { layer, datatype, key, polygons: [], paths: [], texts: [], shapeCount: 0 };
      layers.set(key, entry);
    }
    return entry;
  }

  function growPoint(x: number, y: number): void {
    if (x < minX) minX = x;
    if (x > maxX) maxX = x;
    if (y < minY) minY = y;
    if (y > maxY) maxY = y;
  }

  function growBbox(points: number[]): void {
    for (let i = 0; i + 1 < points.length; i += 2) growPoint(points[i], points[i + 1]);
  }

  /**
   * Grows the bbox by a path's *drawn* extent: half its width perpendicular
   * to each segment, plus an end cap only when the path type has one. Padding
   * every vertex by half-width in both axes instead would overstate a flush
   * (pathtype 0) path's length by half a width at each end — enough to
   * disagree with KLayout's bounding box on real standard cells, whose power
   * rails are exactly such paths.
   */
  function growBboxForPath(points: number[], width: number, pathtype: number): void {
    const half = Math.abs(width) / 2;
    if (points.length < 4 || half === 0) {
      growBbox(points);
      return;
    }
    const capExtension = pathtype === 2 ? half : 0;
    for (let i = 0; i + 3 < points.length; i += 2) {
      const x0 = points[i];
      const y0 = points[i + 1];
      const x1 = points[i + 2];
      const y1 = points[i + 3];
      const length = Math.hypot(x1 - x0, y1 - y0);
      if (length === 0) {
        growPoint(x0, y0);
        continue;
      }
      const ux = (x1 - x0) / length;
      const uy = (y1 - y0) / length;
      const nx = -uy * half;
      const ny = ux * half;
      // Extend only the first/last vertex by the cap; interior joins are
      // already covered by the neighbouring segments' corners.
      const startExt = i === 0 ? capExtension : 0;
      const endExt = i + 4 >= points.length ? capExtension : 0;
      const ax = x0 - ux * startExt;
      const ay = y0 - uy * startExt;
      const bx = x1 + ux * endExt;
      const by = y1 + uy * endExt;
      growPoint(ax + nx, ay + ny);
      growPoint(ax - nx, ay - ny);
      growPoint(bx + nx, by + ny);
      growPoint(bx - nx, by - ny);
    }
    if (pathtype === 1) {
      // Round caps bulge half a width past each end point in every direction.
      growPoint(points[0] - half, points[1] - half);
      growPoint(points[0] + half, points[1] + half);
      const lastX = points[points.length - 2];
      const lastY = points[points.length - 1];
      growPoint(lastX - half, lastY - half);
      growPoint(lastX + half, lastY + half);
    }
  }

  function walk(structure: GdsStructure, transform: Affine, depth: number, seen: Set<string>): void {
    if (depth > maxDepth || truncated) {
      truncated = truncated || depth > maxDepth;
      return;
    }
    for (const element of structure.elements) {
      if (shapeCount >= maxShapes) {
        truncated = true;
        return;
      }
      switch (element.kind) {
        case "boundary":
        case "box": {
          const points = transformPoints(element.xy, transform);
          layerFor(element.layer, element.datatype).polygons.push(points);
          layerFor(element.layer, element.datatype).shapeCount += 1;
          growBbox(points);
          shapeCount += 1;
          break;
        }
        case "path": {
          const points = transformPoints(element.xy, transform);
          const width = element.width * affineScale(transform);
          const entry = layerFor(element.layer, element.datatype);
          entry.paths.push({ points, width, pathtype: element.pathtype });
          entry.shapeCount += 1;
          growBboxForPath(points, width, element.pathtype);
          shapeCount += 1;
          break;
        }
        case "text": {
          if (textCount >= maxTexts) break;
          const [x, y] = transformPoints([element.x, element.y], transform);
          layerFor(element.layer, element.datatype).texts.push({ x, y, text: element.text });
          textCount += 1;
          break;
        }
        case "sref": {
          const child = library.structures.get(element.sname);
          if (!child || seen.has(element.sname)) break; // missing or cyclic
          const next = composeAffine(
            transform,
            refTransform(element.reflect, element.mag, element.angle, element.x, element.y),
          );
          seen.add(element.sname);
          walk(child, next, depth + 1, seen);
          seen.delete(element.sname);
          break;
        }
        case "aref": {
          const child = library.structures.get(element.sname);
          if (!child || seen.has(element.sname)) break;
          const [x0, y0, xCol, yCol, xRow, yRow] = element.xy;
          const colStep = {
            x: (xCol - x0) / element.cols,
            y: (yCol - y0) / element.cols,
          };
          const rowStep = {
            x: (xRow - x0) / element.rows,
            y: (yRow - y0) / element.rows,
          };
          seen.add(element.sname);
          for (let row = 0; row < element.rows && !truncated; row += 1) {
            for (let col = 0; col < element.cols && !truncated; col += 1) {
              if (shapeCount >= maxShapes) {
                truncated = true;
                break;
              }
              const base = refTransform(element.reflect, element.mag, element.angle, 0, 0);
              base.e = x0 + col * colStep.x + row * rowStep.x;
              base.f = y0 + col * colStep.y + row * rowStep.y;
              walk(child, composeAffine(transform, base), depth + 1, seen);
            }
          }
          seen.delete(element.sname);
          break;
        }
        default:
          break;
      }
    }
  }

  const top = library.structures.get(topName);
  if (top) walk(top, IDENTITY, 0, new Set([topName]));

  const sorted = [...layers.values()]
    // Drop layers that only ever held a skipped element kind.
    .filter((entry) => entry.shapeCount > 0 || entry.texts.length > 0)
    .sort((a, b) => (a.layer !== b.layer ? a.layer - b.layer : a.datatype - b.datatype));

  return {
    topName,
    dbuMicrons: library.dbuMicrons,
    bbox:
      minX === Infinity
        ? null
        : { minX, minY, maxX, maxY },
    layers: sorted,
    shapeCount,
    truncated,
  };
}
