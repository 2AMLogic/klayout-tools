- **`klt drc`: `enclosed`/`enclosing` markers list `polygon` in perimeter
  order** (#7979): KLayout pairs the two edges of an enclosure check in the
  same direction, so the four corners of the marker were listed in a
  self-crossing order (consecutive vertices ran along a diagonal) and
  `violations[].polygon` could not be used as an outline -- rendered,
  measured, or built into a `kdb.Polygon`. The same four corners are now
  listed around the quad's perimeter, for every deck's `enclosed`/`enclosing`
  rule (e.g. gf180mcu `nplus.enclosing.comp.1`). Vertex order only: `bbox`,
  the corner set, `space`/`width`/`separation` markers, two-point segments
  and `null` polygons are unchanged, and no field is added, removed or
  retyped. The starting vertex and winding are not specified. The committed
  `examples/drc/example.drc.json` is regenerated for its four
  `diff.enclosing.licon.1` markers.
