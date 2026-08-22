/**
 * Unit tests for the client-side GDSII reader (issue #943 / #1284).
 *
 * Fixtures are built byte-by-byte with `gdsFixture.ts`'s writer so each
 * expectation is traceable to an explicit record in the stream. The
 * real-file counterpart (parsing the gallery's own committed `.gds` blocks
 * and comparing against KLayout-derived numbers) lives in
 * `realBlocks.test.ts`.
 */
import { describe, expect, it } from "vitest";
import { GdsParseError, findTopStructure, parseGds, readReal8 } from "./parseGds";
import { GdsWriter, buildTwoLevelFixture, encodeReal8, squareXY } from "./gdsFixture";

function reals(values: number[]): number[] {
  return values.map((value) => {
    const bytes = encodeReal8(value);
    return readReal8(new DataView(bytes.buffer), 0);
  });
}

describe("readReal8", () => {
  it("round-trips the excess-64 base-16 real encoding", () => {
    expect(reals([0])).toEqual([0]);
    expect(reals([1])[0]).toBeCloseTo(1, 12);
    expect(reals([-1])[0]).toBeCloseTo(-1, 12);
    expect(reals([1e-3])[0]).toBeCloseTo(1e-3, 12);
    expect(reals([1e-9])[0]).toBeCloseTo(1e-9, 18);
    expect(reals([90])[0]).toBeCloseTo(90, 10);
    expect(reals([0.5])[0]).toBeCloseTo(0.5, 12);
  });
});

describe("parseGds", () => {
  it("reads library units as microns per database unit", () => {
    const library = parseGds(buildTwoLevelFixture());
    expect(library.libname).toBe("FIXTURE.DB");
    expect(library.dbuMicrons).toBeCloseTo(0.001, 9);
    expect(library.userUnitsPerDbu).toBeCloseTo(0.001, 9);
  });

  it("reads structures with their elements", () => {
    const library = parseGds(buildTwoLevelFixture());
    expect([...library.structures.keys()]).toEqual(["CELL", "TOP"]);

    const cell = library.structures.get("CELL");
    expect(cell?.elements.map((element) => element.kind)).toEqual([
      "boundary",
      "path",
      "text",
    ]);

    const boundary = cell?.elements[0];
    expect(boundary).toMatchObject({
      kind: "boundary",
      layer: 68,
      datatype: 20,
      xy: squareXY(0, 0, 1000),
    });

    const path = cell?.elements[1];
    expect(path).toMatchObject({
      kind: "path",
      layer: 67,
      datatype: 20,
      width: 200,
      pathtype: 1,
      xy: [0, 500, 2000, 500],
    });

    const text = cell?.elements[2];
    expect(text).toMatchObject({ kind: "text", layer: 68, datatype: 5, text: "VDD" });
  });

  it("reads SREF transforms and AREF lattices", () => {
    const library = parseGds(buildTwoLevelFixture());
    const top = library.structures.get("TOP");
    const [, plain, rotated, array] = top?.elements ?? [];

    expect(plain).toMatchObject({ kind: "sref", sname: "CELL", x: 0, y: 0, angle: 0, mag: 1 });
    expect(rotated).toMatchObject({ kind: "sref", sname: "CELL", x: 5000, y: 0 });
    expect((rotated as { angle: number }).angle).toBeCloseTo(90, 9);
    expect(array).toMatchObject({
      kind: "aref",
      sname: "CELL",
      cols: 2,
      rows: 3,
      xy: [0, 5000, 4000, 5000, 0, 11000],
    });
  });

  it("reads a reflected reference's STRANS bit", () => {
    const buffer = new GdsWriter()
      .header()
      .beginStructure("CELL")
      .boundary(1, 0, squareXY(0, 0, 100))
      .endStructure()
      .beginStructure("TOP")
      .sref("CELL", 0, 0, { reflect: true, mag: 2 })
      .endStructure()
      .end();

    const reference = parseGds(buffer).structures.get("TOP")?.elements[0];
    expect(reference).toMatchObject({ kind: "sref", reflect: true });
    expect((reference as { mag: number }).mag).toBeCloseTo(2, 9);
  });

  it("tolerates the 2048-byte NUL block padding real writers append", () => {
    const stream = new Uint8Array(buildTwoLevelFixture());
    const padded = new Uint8Array(stream.length + 512);
    padded.set(stream, 0);
    expect(parseGds(padded.buffer).structures.size).toBe(2);
  });

  it("throws a GdsParseError on input that is not a GDSII stream", () => {
    const notGds = new TextEncoder().encode("this is definitely not a layout").buffer;
    expect(() => parseGds(notGds)).toThrow(GdsParseError);
  });

  it("throws a GdsParseError on a header with no structures", () => {
    expect(() => parseGds(new GdsWriter().header().end())).toThrow(/no structures/);
  });
});

describe("findTopStructure", () => {
  it("picks the structure nothing references", () => {
    expect(findTopStructure(parseGds(buildTwoLevelFixture()))).toBe("TOP");
  });

  it("prefers the largest root when several structures are unreferenced", () => {
    const buffer = new GdsWriter()
      .header()
      .beginStructure("SMALL")
      .boundary(1, 0, squareXY(0, 0, 10))
      .endStructure()
      .beginStructure("BIG")
      .boundary(1, 0, squareXY(0, 0, 10))
      .boundary(1, 0, squareXY(20, 0, 10))
      .endStructure()
      .end();

    expect(findTopStructure(parseGds(buffer))).toBe("BIG");
  });
});
