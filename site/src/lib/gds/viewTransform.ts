/**
 * Pan/zoom view math for the embedded GDS viewer (issue #943 / #1284).
 *
 * Kept out of the React component so the "user-navigable" half of the
 * viewer — fit-to-view, zoom about a cursor, drag-pan — is plain, testable
 * arithmetic rather than something only observable through a canvas mock.
 *
 * Screen space is CSS pixels with y growing downward; world space is GDSII
 * database units with y growing upward, hence the sign flip:
 *
 *   screenX =  worldX * scale + tx
 *   screenY = -worldY * scale + ty
 */
import type { Bbox } from "./flattenGds";

export interface View {
  /** Screen pixels per database unit. */
  scale: number;
  tx: number;
  ty: number;
}

/** Fraction of the surface the fitted layout occupies (leaves a margin). */
const FIT_PADDING = 0.92;

export function fitView(bbox: Bbox | null, width: number, height: number): View {
  if (!bbox || width <= 0 || height <= 0) {
    return { scale: 1, tx: width / 2, ty: height / 2 };
  }
  const spanX = Math.max(bbox.maxX - bbox.minX, 1);
  const spanY = Math.max(bbox.maxY - bbox.minY, 1);
  const scale = Math.min(width / spanX, height / spanY) * FIT_PADDING;
  const centerX = (bbox.minX + bbox.maxX) / 2;
  const centerY = (bbox.minY + bbox.maxY) / 2;
  return {
    scale,
    tx: width / 2 - centerX * scale,
    ty: height / 2 + centerY * scale,
  };
}

export function worldToScreen(view: View, x: number, y: number): [number, number] {
  return [x * view.scale + view.tx, -y * view.scale + view.ty];
}

export function screenToWorld(view: View, x: number, y: number): [number, number] {
  return [(x - view.tx) / view.scale, (view.ty - y) / view.scale];
}

/**
 * Scales about a screen-space anchor, keeping whatever world point is under
 * that anchor exactly where it is (the behavior that makes wheel-zoom feel
 * like zooming "into the cursor" rather than into the centre).
 */
export function zoomView(
  view: View,
  factor: number,
  anchorX: number,
  anchorY: number,
  minScale: number,
  maxScale: number,
): View {
  const scale = Math.min(maxScale, Math.max(minScale, view.scale * factor));
  const [worldX, worldY] = screenToWorld(view, anchorX, anchorY);
  return { scale, tx: anchorX - worldX * scale, ty: anchorY + worldY * scale };
}

export function panView(view: View, dx: number, dy: number): View {
  return { ...view, tx: view.tx + dx, ty: view.ty + dy };
}
