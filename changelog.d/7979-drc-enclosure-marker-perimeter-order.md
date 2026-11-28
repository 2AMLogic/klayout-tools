- **DRC marker perimeter order** (#7979). When an enclosure check produces
  two edges in the same direction, `klt drc` now reports the outline vertices
  in perimeter order. This fixes the crossed quadrilaterals reported by
  gf180mcu `nplus.enclosing.comp.1`. Parallel edges retain all four corners.
  A chamfer or a retraced side can produce a triangle when the outline omits
  an intermediate collinear point. The report keeps its marker bounding boxes, two-point segments and
  null polygons. Existing simple rings keep their vertex order. The four `diff.enclosing.licon.1` markers in
  `examples/drc/example.drc.json` show the corrected order.
