/**
 * Test-only GDSII stream writer (issue #943 / #1284).
 *
 * `parseGds()` is a binary reader, so its tests need real bytes. Committing
 * a binary fixture would make the expected content unreadable in review;
 * building the stream here keeps every fixture's structure explicit in
 * source. Only imported by tests — never referenced from shipped components,
 * so it never reaches a browser bundle.
 */

interface RecordChunk {
  type: number;
  dataType: number;
  bytes: Uint8Array;
}

const EMPTY = new Uint8Array(0);

/** Encodes an 8-byte GDSII (excess-64, base-16) real. */
export function encodeReal8(value: number): Uint8Array {
  const bytes = new Uint8Array(8);
  if (value === 0) return bytes;
  let magnitude = Math.abs(value);
  let exponent = 0;
  while (magnitude >= 1) {
    magnitude /= 16;
    exponent += 1;
  }
  while (magnitude < 1 / 16) {
    magnitude *= 16;
    exponent -= 1;
  }
  bytes[0] = (value < 0 ? 0x80 : 0) | ((exponent + 64) & 0x7f);
  let fraction = magnitude;
  for (let i = 1; i < 8; i += 1) {
    fraction *= 256;
    const byte = Math.floor(fraction);
    bytes[i] = byte;
    fraction -= byte;
  }
  return bytes;
}

function int2(values: number[]): Uint8Array {
  const bytes = new Uint8Array(values.length * 2);
  const view = new DataView(bytes.buffer);
  values.forEach((value, index) => view.setInt16(index * 2, value));
  return bytes;
}

function int4(values: number[]): Uint8Array {
  const bytes = new Uint8Array(values.length * 4);
  const view = new DataView(bytes.buffer);
  values.forEach((value, index) => view.setInt32(index * 4, value));
  return bytes;
}

function ascii(text: string): Uint8Array {
  const padded = text.length % 2 === 0 ? text : `${text}\0`;
  const bytes = new Uint8Array(padded.length);
  for (let i = 0; i < padded.length; i += 1) bytes[i] = padded.charCodeAt(i);
  return bytes;
}

/** A tiny fluent builder for a GDSII stream. */
export class GdsWriter {
  private chunks: RecordChunk[] = [];

  record(type: number, dataType: number, bytes: Uint8Array = EMPTY): this {
    this.chunks.push({ type, dataType, bytes });
    return this;
  }

  header(dbuMicrons = 0.001, userUnits = 0.001): this {
    this.record(0x00, 2, int2([600]));
    this.record(0x01, 2, int2([2026, 8, 22, 0, 0, 0, 2026, 8, 22, 0, 0, 0]));
    this.record(0x02, 6, ascii("FIXTURE.DB"));
    const units = new Uint8Array(16);
    units.set(encodeReal8(userUnits), 0);
    units.set(encodeReal8(dbuMicrons * 1e-6), 8);
    return this.record(0x03, 5, units);
  }

  beginStructure(name: string): this {
    this.record(0x05, 2, int2([2026, 8, 22, 0, 0, 0, 2026, 8, 22, 0, 0, 0]));
    return this.record(0x06, 6, ascii(name));
  }

  endStructure(): this {
    return this.record(0x07, 0);
  }

  boundary(layer: number, datatype: number, xy: number[]): this {
    this.record(0x08, 0);
    this.record(0x0d, 2, int2([layer]));
    this.record(0x0e, 2, int2([datatype]));
    this.record(0x10, 3, int4(xy));
    return this.record(0x11, 0);
  }

  path(layer: number, datatype: number, width: number, pathtype: number, xy: number[]): this {
    this.record(0x09, 0);
    this.record(0x0d, 2, int2([layer]));
    this.record(0x0e, 2, int2([datatype]));
    this.record(0x21, 2, int2([pathtype]));
    this.record(0x0f, 3, int4([width]));
    this.record(0x10, 3, int4(xy));
    return this.record(0x11, 0);
  }

  text(layer: number, texttype: number, x: number, y: number, value: string): this {
    this.record(0x0c, 0);
    this.record(0x0d, 2, int2([layer]));
    this.record(0x16, 2, int2([texttype]));
    this.record(0x10, 3, int4([x, y]));
    this.record(0x19, 6, ascii(value));
    return this.record(0x11, 0);
  }

  sref(
    sname: string,
    x: number,
    y: number,
    options: { reflect?: boolean; mag?: number; angle?: number } = {},
  ): this {
    this.record(0x0a, 0);
    this.record(0x12, 6, ascii(sname));
    if (options.reflect || options.mag !== undefined || options.angle !== undefined) {
      this.record(0x1a, 1, int2([options.reflect ? -32768 : 0]));
    }
    if (options.mag !== undefined) this.record(0x1b, 5, encodeReal8(options.mag));
    if (options.angle !== undefined) this.record(0x1c, 5, encodeReal8(options.angle));
    this.record(0x10, 3, int4([x, y]));
    return this.record(0x11, 0);
  }

  aref(sname: string, cols: number, rows: number, xy: number[]): this {
    this.record(0x0b, 0);
    this.record(0x12, 6, ascii(sname));
    this.record(0x13, 2, int2([cols, rows]));
    this.record(0x10, 3, int4(xy));
    return this.record(0x11, 0);
  }

  end(): ArrayBuffer {
    this.record(0x04, 0);
    const total = this.chunks.reduce((sum, chunk) => sum + 4 + chunk.bytes.length, 0);
    const out = new Uint8Array(total);
    const view = new DataView(out.buffer);
    let offset = 0;
    for (const chunk of this.chunks) {
      view.setUint16(offset, 4 + chunk.bytes.length);
      view.setUint8(offset + 2, chunk.type);
      view.setUint8(offset + 3, chunk.dataType);
      out.set(chunk.bytes, offset + 4);
      offset += 4 + chunk.bytes.length;
    }
    return out.buffer;
  }
}

/** A 1 µm square on `layer/datatype`, in 1 nm database units. */
export function squareXY(x: number, y: number, size: number): number[] {
  return [x, y, x + size, y, x + size, y + size, x, y + size, x, y];
}

/**
 * Two-level fixture: a `CELL` with one square + one path + one label, placed
 * in `TOP` once directly, once rotated 90°, and once as a 2×3 array.
 */
export function buildTwoLevelFixture(): ArrayBuffer {
  const writer = new GdsWriter();
  writer.header();
  writer
    .beginStructure("CELL")
    .boundary(68, 20, squareXY(0, 0, 1000))
    .path(67, 20, 200, 1, [0, 500, 2000, 500])
    .text(68, 5, 100, 100, "VDD")
    .endStructure();
  writer
    .beginStructure("TOP")
    .boundary(64, 20, squareXY(-500, -500, 10000))
    .sref("CELL", 0, 0)
    .sref("CELL", 5000, 0, { angle: 90 })
    .aref("CELL", 2, 3, [0, 5000, 4000, 5000, 0, 11000])
    .endStructure();
  return writer.end();
}
