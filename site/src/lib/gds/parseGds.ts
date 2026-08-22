/**
 * Minimal, dependency-free GDSII stream parser (issue #943 / #1284).
 *
 * The gallery's embedded viewer renders a block's *actual* GDS in the
 * browser, same-origin, with no third-party rendering service and no
 * precomputed conversion artifact: `copy-renders.mjs` already stages the raw
 * `blocks/<slug>/output/<layout_file>` at `/blocks/<slug>/<file>.gds`, so the
 * only missing piece was a client-side reader. This module is that reader —
 * it turns the binary stream into structures + elements; `flattenGds.ts` then
 * resolves the hierarchy into drawable shapes.
 *
 * Scope is deliberately "what a viewer needs", not "a complete GDSII
 * implementation": BOUNDARY, PATH, BOX, SREF, AREF, and TEXT are read;
 * NODE and unrecognised records are skipped rather than treated as errors,
 * matching how KLayout tolerates vendor extensions. Everything is returned in
 * database units (integers); callers scale by `dbuMicrons`.
 *
 * Format reference: the GDSII Stream Format specification (record = 2-byte
 * big-endian length including the 4-byte header, 1-byte record type, 1-byte
 * data type, then payload).
 */

/** GDSII record types this parser acts on. */
const REC = {
  HEADER: 0x00,
  BGNLIB: 0x01,
  LIBNAME: 0x02,
  UNITS: 0x03,
  ENDLIB: 0x04,
  BGNSTR: 0x05,
  STRNAME: 0x06,
  ENDSTR: 0x07,
  BOUNDARY: 0x08,
  PATH: 0x09,
  SREF: 0x0a,
  AREF: 0x0b,
  TEXT: 0x0c,
  LAYER: 0x0d,
  DATATYPE: 0x0e,
  WIDTH: 0x0f,
  XY: 0x10,
  ENDEL: 0x11,
  SNAME: 0x12,
  COLROW: 0x13,
  NODE: 0x15,
  TEXTTYPE: 0x16,
  STRING: 0x19,
  STRANS: 0x1a,
  MAG: 0x1b,
  ANGLE: 0x1c,
  PATHTYPE: 0x21,
  BOX: 0x2d,
  BOXTYPE: 0x2e,
  BGNEXTN: 0x30,
  ENDEXTN: 0x31,
} as const;

export interface GdsBoundary {
  kind: "boundary";
  layer: number;
  datatype: number;
  /** Flat `[x0, y0, x1, y1, ...]` in database units. */
  xy: number[];
}

export interface GdsPath {
  kind: "path";
  layer: number;
  datatype: number;
  /** Centerline `[x0, y0, ...]` in database units. */
  xy: number[];
  /** Full width in database units (0 when the record omits WIDTH). */
  width: number;
  /** 0 = butt, 1 = round, 2 = square (half-width extension), 4 = custom. */
  pathtype: number;
  /** Custom start/end extensions, only meaningful for `pathtype === 4`. */
  bgnextn: number;
  endextn: number;
}

export interface GdsBoxElement {
  kind: "box";
  layer: number;
  datatype: number;
  xy: number[];
}

export interface GdsTextElement {
  kind: "text";
  layer: number;
  datatype: number;
  x: number;
  y: number;
  text: string;
}

export interface GdsTransform {
  /** STRANS bit 15 — reflect about the x-axis before rotation. */
  reflect: boolean;
  /** Magnification (default 1). */
  mag: number;
  /** Rotation in degrees, counter-clockwise (default 0). */
  angle: number;
}

export interface GdsSref extends GdsTransform {
  kind: "sref";
  sname: string;
  x: number;
  y: number;
}

export interface GdsAref extends GdsTransform {
  kind: "aref";
  sname: string;
  cols: number;
  rows: number;
  /** Array reference anchor and the two lattice end points, per the spec. */
  xy: number[];
}

export type GdsElement =
  | GdsBoundary
  | GdsPath
  | GdsBoxElement
  | GdsTextElement
  | GdsSref
  | GdsAref;

export interface GdsStructure {
  name: string;
  elements: GdsElement[];
}

export interface GdsLibrary {
  libname: string;
  /** Microns per database unit (e.g. 0.001 for a 1 nm grid). */
  dbuMicrons: number;
  /** Database units per user unit, straight from the UNITS record. */
  userUnitsPerDbu: number;
  structures: Map<string, GdsStructure>;
}

export class GdsParseError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "GdsParseError";
  }
}

/**
 * Decodes an 8-byte GDSII real: sign bit, 7-bit excess-64 base-16 exponent,
 * 56-bit fraction. (Not IEEE 754 — this predates it.)
 */
export function readReal8(view: DataView, offset: number): number {
  const first = view.getUint8(offset);
  const sign = (first & 0x80) === 0 ? 1 : -1;
  const exponent = (first & 0x7f) - 64;
  let mantissa = 0;
  for (let i = 1; i < 8; i += 1) {
    mantissa = mantissa * 256 + view.getUint8(offset + i);
  }
  if (mantissa === 0) return 0;
  return sign * mantissa * Math.pow(16, exponent) * Math.pow(2, -56);
}

function readAscii(view: DataView, offset: number, length: number): string {
  let out = "";
  for (let i = 0; i < length; i += 1) {
    const code = view.getUint8(offset + i);
    if (code === 0) break; // odd-length strings are NUL-padded
    out += String.fromCharCode(code);
  }
  return out;
}

function readInt2Array(view: DataView, offset: number, length: number): number[] {
  const out: number[] = [];
  for (let i = 0; i + 1 < length; i += 2) out.push(view.getInt16(offset + i));
  return out;
}

function readInt4Array(view: DataView, offset: number, length: number): number[] {
  const out: number[] = [];
  for (let i = 0; i + 3 < length; i += 4) out.push(view.getInt32(offset + i));
  return out;
}

/** Element-under-construction: fields arrive as separate records before ENDEL. */
interface PendingElement {
  kind: GdsElement["kind"];
  layer: number;
  datatype: number;
  xy: number[];
  width: number;
  pathtype: number;
  bgnextn: number;
  endextn: number;
  sname: string;
  cols: number;
  rows: number;
  reflect: boolean;
  mag: number;
  angle: number;
  text: string;
}

function newPending(kind: GdsElement["kind"]): PendingElement {
  return {
    kind,
    layer: 0,
    datatype: 0,
    xy: [],
    width: 0,
    pathtype: 0,
    bgnextn: 0,
    endextn: 0,
    sname: "",
    cols: 1,
    rows: 1,
    reflect: false,
    mag: 1,
    angle: 0,
    text: "",
  };
}

function finishElement(pending: PendingElement): GdsElement | undefined {
  switch (pending.kind) {
    case "boundary":
      if (pending.xy.length < 6) return undefined;
      return {
        kind: "boundary",
        layer: pending.layer,
        datatype: pending.datatype,
        xy: pending.xy,
      };
    case "path":
      if (pending.xy.length < 4) return undefined;
      return {
        kind: "path",
        layer: pending.layer,
        datatype: pending.datatype,
        xy: pending.xy,
        width: pending.width,
        pathtype: pending.pathtype,
        bgnextn: pending.bgnextn,
        endextn: pending.endextn,
      };
    case "box":
      if (pending.xy.length < 8) return undefined;
      return {
        kind: "box",
        layer: pending.layer,
        datatype: pending.datatype,
        xy: pending.xy,
      };
    case "text":
      if (pending.xy.length < 2) return undefined;
      return {
        kind: "text",
        layer: pending.layer,
        datatype: pending.datatype,
        x: pending.xy[0],
        y: pending.xy[1],
        text: pending.text,
      };
    case "sref":
      if (pending.xy.length < 2 || !pending.sname) return undefined;
      return {
        kind: "sref",
        sname: pending.sname,
        x: pending.xy[0],
        y: pending.xy[1],
        reflect: pending.reflect,
        mag: pending.mag,
        angle: pending.angle,
      };
    case "aref":
      if (pending.xy.length < 6 || !pending.sname) return undefined;
      return {
        kind: "aref",
        sname: pending.sname,
        cols: Math.max(1, pending.cols),
        rows: Math.max(1, pending.rows),
        xy: pending.xy,
        reflect: pending.reflect,
        mag: pending.mag,
        angle: pending.angle,
      };
    default:
      return undefined;
  }
}

/**
 * Parses a GDSII stream into its structures.
 *
 * Throws `GdsParseError` only for input that cannot be a GDS stream at all
 * (no HEADER/BGNLIB record, or no structures); a truncated tail is tolerated
 * so a partially-transferred file still shows what it does contain.
 */
export function parseGds(buffer: ArrayBuffer): GdsLibrary {
  const view = new DataView(buffer);
  const structures = new Map<string, GdsStructure>();
  let libname = "";
  let userUnitsPerDbu = 1e-3;
  let dbuMicrons = 1e-3;
  let sawHeader = false;

  let current: GdsStructure | undefined;
  let pending: PendingElement | undefined;

  let offset = 0;
  while (offset + 4 <= view.byteLength) {
    const length = view.getUint16(offset);
    // GDS files are padded to a 2048-byte block with NULs; a zero/short
    // length is the padding, not a malformed record.
    if (length < 4 || offset + length > view.byteLength) break;
    const recordType = view.getUint8(offset + 2);
    const dataOffset = offset + 4;
    const dataLength = length - 4;

    switch (recordType) {
      case REC.HEADER:
      case REC.BGNLIB:
        sawHeader = true;
        break;
      case REC.LIBNAME:
        libname = readAscii(view, dataOffset, dataLength);
        break;
      case REC.UNITS:
        if (dataLength >= 16) {
          userUnitsPerDbu = readReal8(view, dataOffset);
          // Second value is meters per database unit.
          dbuMicrons = readReal8(view, dataOffset + 8) * 1e6;
        }
        break;
      case REC.BGNSTR:
        current = { name: "", elements: [] };
        break;
      case REC.STRNAME:
        if (current) {
          current.name = readAscii(view, dataOffset, dataLength);
          structures.set(current.name, current);
        }
        break;
      case REC.ENDSTR:
        current = undefined;
        break;
      case REC.BOUNDARY:
        pending = newPending("boundary");
        break;
      case REC.PATH:
        pending = newPending("path");
        break;
      case REC.BOX:
        pending = newPending("box");
        break;
      case REC.TEXT:
        pending = newPending("text");
        break;
      case REC.SREF:
        pending = newPending("sref");
        break;
      case REC.AREF:
        pending = newPending("aref");
        break;
      case REC.NODE:
        // Recognised but not drawable — consume its records without emitting.
        pending = undefined;
        break;
      case REC.LAYER:
        if (pending) pending.layer = view.getInt16(dataOffset);
        break;
      case REC.DATATYPE:
      case REC.BOXTYPE:
      case REC.TEXTTYPE:
        if (pending) pending.datatype = view.getInt16(dataOffset);
        break;
      case REC.WIDTH:
        if (pending) pending.width = view.getInt32(dataOffset);
        break;
      case REC.PATHTYPE:
        if (pending) pending.pathtype = view.getInt16(dataOffset);
        break;
      case REC.BGNEXTN:
        if (pending) pending.bgnextn = view.getInt32(dataOffset);
        break;
      case REC.ENDEXTN:
        if (pending) pending.endextn = view.getInt32(dataOffset);
        break;
      case REC.XY:
        if (pending) pending.xy = readInt4Array(view, dataOffset, dataLength);
        break;
      case REC.SNAME:
        if (pending) pending.sname = readAscii(view, dataOffset, dataLength);
        break;
      case REC.COLROW:
        if (pending) {
          const [cols, rows] = readInt2Array(view, dataOffset, dataLength);
          pending.cols = cols ?? 1;
          pending.rows = rows ?? 1;
        }
        break;
      case REC.STRANS:
        if (pending) pending.reflect = (view.getUint16(dataOffset) & 0x8000) !== 0;
        break;
      case REC.MAG:
        if (pending && dataLength >= 8) pending.mag = readReal8(view, dataOffset);
        break;
      case REC.ANGLE:
        if (pending && dataLength >= 8) pending.angle = readReal8(view, dataOffset);
        break;
      case REC.STRING:
        if (pending) pending.text = readAscii(view, dataOffset, dataLength);
        break;
      case REC.ENDEL: {
        if (pending && current) {
          const element = finishElement(pending);
          if (element) current.elements.push(element);
        }
        pending = undefined;
        break;
      }
      case REC.ENDLIB:
        offset = view.byteLength; // stop; trailing bytes are block padding
        break;
      default:
        break; // unknown/uninteresting record — skip its payload
    }

    if (offset >= view.byteLength) break;
    offset += length;
  }

  if (!sawHeader) {
    throw new GdsParseError("not a GDSII stream (no HEADER/BGNLIB record found)");
  }
  if (structures.size === 0) {
    throw new GdsParseError("GDSII stream contains no structures");
  }

  return { libname, dbuMicrons, userUnitsPerDbu, structures };
}

/**
 * Returns the name of the structure that nothing else references — the
 * conventional "top cell". Falls back to the last structure defined (GDSII
 * writes children before parents) when every structure is referenced, which
 * only happens in cyclic or partial files.
 */
export function findTopStructure(library: GdsLibrary): string | undefined {
  const referenced = new Set<string>();
  for (const structure of library.structures.values()) {
    for (const element of structure.elements) {
      if (element.kind === "sref" || element.kind === "aref") {
        referenced.add(element.sname);
      }
    }
  }
  const names = [...library.structures.keys()];
  const roots = names.filter((name) => !referenced.has(name));
  if (roots.length === 0) return names[names.length - 1];
  if (roots.length === 1) return roots[0];
  // Multiple roots: prefer the one with the most elements, then by name so
  // the choice is deterministic across runs.
  return roots.sort((a, b) => {
    const sizeDelta =
      (library.structures.get(b)?.elements.length ?? 0) -
      (library.structures.get(a)?.elements.length ?? 0);
    return sizeDelta !== 0 ? sizeDelta : a.localeCompare(b);
  })[0];
}
