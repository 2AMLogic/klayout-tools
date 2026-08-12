/**
 * Types for the mesh + field data `FieldViewer` renders (issue #841).
 *
 * Generic enough for any mesh+field source — a hand-authored fixture, a
 * future `klt` layout mesh — but shaped to match the geode-fem browser-EM
 * epic's (#840) Phase-1a export format so #850 can feed it directly without
 * a translation layer. `FieldViewer` itself has no dependency on geode-fem;
 * this is just the data shape it expects.
 */

/**
 * A 3D surface mesh: vertex positions plus cells (polygons over those
 * vertices). Cells are not required to be triangles — `FieldViewer`
 * fan-triangulates each cell for rendering (see `fieldMath.ts`'s
 * `triangulateCells`), so a BEM/FEM export's native quad or n-gon cells
 * don't need pre-triangulating.
 */
export interface FieldMesh {
  /** Vertex positions, one `[x, y, z]` triple per vertex. */
  vertices: number[][];
  /** Cells (polygons) as index arrays into `vertices`, each with length
   *  >= 3, in winding order. */
  cells: number[][];
}

/**
 * One swept parameter/frequency point's field sample, aligned 1:1 with
 * `FieldMesh.vertices` — `frame.scalar[i]` / `frame.vector[i]` describes
 * `mesh.vertices[i]`.
 */
export interface FieldFrame {
  /** Display label for this sweep point, e.g. `"2.4 GHz"`. Falls back to
   *  the frame's index when omitted. */
  label?: string;
  /** Per-vertex scalar magnitude, colormapped directly. */
  scalar?: number[];
  /** Per-vertex vector field `[x, y, z]`. Used as the colormap value (via
   *  its magnitude) when `scalar` is omitted. */
  vector?: number[][];
}

/** The full data payload `FieldViewer` consumes — a mesh plus one or more
 *  swept frames, driven entirely by props (no solver dependency). */
export interface FieldViewerData {
  mesh: FieldMesh;
  frames: FieldFrame[];
}
