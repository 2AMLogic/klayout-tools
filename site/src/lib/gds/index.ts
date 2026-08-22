/**
 * Client-side GDSII reading + styling for the gallery's embedded layout
 * viewer (issue #943 / #1284). See `parseGds.ts` (stream -> structures),
 * `flattenGds.ts` (structures -> drawable shapes), and `layerStyle.ts`
 * (PDK-accurate colors/names).
 */
export { parseGds, findTopStructure, readReal8, GdsParseError } from "./parseGds";
export type {
  GdsLibrary,
  GdsStructure,
  GdsElement,
  GdsBoundary,
  GdsPath,
  GdsSref,
  GdsAref,
} from "./parseGds";
export {
  flattenGds,
  composeAffine,
  refTransform,
  affineScale,
  IDENTITY,
} from "./flattenGds";
export type { FlatLayout, FlatLayer, FlatPath, Affine, Bbox } from "./flattenGds";
export { resolveLayerStyle, fallbackLayerColor } from "./layerStyle";
export type { LayerStyle } from "./layerStyle";
export {
  fitView,
  panView,
  screenToWorld,
  worldToScreen,
  zoomView,
} from "./viewTransform";
export type { View } from "./viewTransform";
export { layerNamesFromRenders } from "./layerNames";
export type { PdkFamily } from "./layerNames";
