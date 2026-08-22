import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { PointerEvent as ReactPointerEvent, WheelEvent as ReactWheelEvent } from "react";
import {
  fitView,
  flattenGds,
  panView,
  parseGds,
  resolveLayerStyle,
  worldToScreen,
  zoomView,
} from "@/lib/gds";
import type { FlatLayout, LayerStyle, PdkFamily, View } from "@/lib/gds";

/**
 * Same-origin, client-side GDSII renderer for the block detail page's viewer
 * overlay (issue #943, completed by #1284).
 *
 * Fetches the block's staged `.gds` from klayout-tools.org itself (the file
 * `site/scripts/copy-renders.mjs` already publishes at
 * `/blocks/<slug>/<layout_file>`), parses it with `@/lib/gds`'s dependency-
 * free reader, flattens the hierarchy, and draws it to a 2D canvas with
 * pan/zoom and per-layer visibility toggles. No third-party rendering
 * service, no WASM blob, and no precomputed per-block conversion artifact is
 * involved — which is what lets the acceptance criterion "served from
 * klayout-tools.org" hold literally, and keeps the viewer working if any
 * external service changes or disappears.
 *
 * This module is the viewer's entire weight (parser + flattener + the
 * generated PDK color tables), so `GdsViewer.tsx` pulls it in through
 * `React.lazy()`: a detail page that never opens the viewer never downloads
 * this chunk. It is also why every DOM/`window` touch here lives inside an
 * effect or an event handler — the module is never evaluated during
 * `renderToString` prerendering at all.
 */
export interface GdsCanvasProps {
  /** Same-origin URL of the block's staged GDS file. */
  fileUrl: string;
  /** Block display name, used in status text and the canvas aria-label. */
  displayName: string;
  /** PDK family for layer coloring; omitted styles layers by fallback hue. */
  pdkFamily?: PdkFamily;
  /** `"<layer>/<datatype>" -> name` overrides, from the block's renders map. */
  layerNames?: Record<string, string>;
}

type LoadState =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready"; data: FlatLayout };

const ZOOM_STEP = 1.4;
const MIN_SCALE = 1e-9;
const MAX_SCALE = 1e3;
/** Above this shape count, per-shape outlines are skipped while drawing. */
const OUTLINE_SHAPE_LIMIT = 40_000;
/** Above this count, text labels are skipped (they'd be unreadable anyway). */
const TEXT_LABEL_LIMIT = 400;

function formatMicrons(valueDbu: number, dbuMicrons: number): string {
  const microns = valueDbu * dbuMicrons;
  return microns >= 100 ? microns.toFixed(0) : microns.toFixed(2);
}

/**
 * Draws one frame. Kept as a free function (not a hook) so it can be reasoned
 * about — and exercised — independently of React's render cycle.
 */
export function drawLayout(
  ctx: CanvasRenderingContext2D,
  data: FlatLayout,
  view: View,
  visible: ReadonlySet<string>,
  styles: Map<string, LayerStyle>,
  cssWidth: number,
  cssHeight: number,
  dpr: number,
): void {
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, cssWidth, cssHeight);
  ctx.fillStyle = "#0b0d12";
  ctx.fillRect(0, 0, cssWidth, cssHeight);

  const visibleShapes = data.layers
    .filter((layer) => visible.has(layer.key))
    .reduce((total, layer) => total + layer.shapeCount, 0);
  const drawOutlines = visibleShapes <= OUTLINE_SHAPE_LIMIT;
  const totalTexts = data.layers
    .filter((layer) => visible.has(layer.key))
    .reduce((total, layer) => total + layer.texts.length, 0);
  const drawTexts = totalTexts <= TEXT_LABEL_LIMIT;

  ctx.setTransform(dpr * view.scale, 0, 0, -dpr * view.scale, dpr * view.tx, dpr * view.ty);
  const hairline = 1 / view.scale;

  for (const layer of data.layers) {
    if (!visible.has(layer.key)) continue;
    const style = styles.get(layer.key);
    if (!style) continue;

    if (layer.polygons.length > 0) {
      ctx.beginPath();
      for (const polygon of layer.polygons) {
        if (polygon.length < 6) continue;
        ctx.moveTo(polygon[0], polygon[1]);
        for (let i = 2; i + 1 < polygon.length; i += 2) {
          ctx.lineTo(polygon[i], polygon[i + 1]);
        }
        ctx.closePath();
      }
      ctx.globalAlpha = 0.55;
      ctx.fillStyle = style.fill;
      ctx.fill();
      if (drawOutlines) {
        ctx.globalAlpha = 0.95;
        ctx.strokeStyle = style.frame;
        ctx.lineWidth = hairline;
        ctx.stroke();
      }
    }

    for (const path of layer.paths) {
      if (path.points.length < 4) continue;
      ctx.beginPath();
      ctx.moveTo(path.points[0], path.points[1]);
      for (let i = 2; i + 1 < path.points.length; i += 2) {
        ctx.lineTo(path.points[i], path.points[i + 1]);
      }
      ctx.globalAlpha = 0.75;
      ctx.strokeStyle = style.fill;
      ctx.lineWidth = Math.max(path.width, hairline);
      // GDSII pathtype: 0 = flush, 1 = round, 2 = square half-width
      // extension. Type 4 (custom BGNEXTN/ENDEXTN) is drawn flush.
      ctx.lineCap = path.pathtype === 1 ? "round" : path.pathtype === 2 ? "square" : "butt";
      ctx.lineJoin = "round";
      ctx.stroke();
    }
  }

  ctx.globalAlpha = 1;
  ctx.lineCap = "butt";

  if (drawTexts) {
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.font = "11px ui-monospace, monospace";
    ctx.textBaseline = "middle";
    for (const layer of data.layers) {
      if (!visible.has(layer.key) || layer.texts.length === 0) continue;
      const style = styles.get(layer.key);
      if (!style) continue;
      ctx.fillStyle = style.frame;
      for (const label of layer.texts) {
        const [x, y] = worldToScreen(view, label.x, label.y);
        if (x < -50 || y < -20 || x > cssWidth + 50 || y > cssHeight + 20) continue;
        ctx.fillText(label.text, x + 3, y);
      }
    }
  }
  ctx.setTransform(1, 0, 0, 1, 0, 0);
}

export default function GdsCanvas({
  fileUrl,
  displayName,
  pdkFamily,
  layerNames,
}: GdsCanvasProps) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const containerRef = useRef<HTMLDivElement | null>(null);
  const dragRef = useRef<{ x: number; y: number } | null>(null);
  const [state, setState] = useState<LoadState>({ status: "loading" });
  const [view, setView] = useState<View | null>(null);
  const [hidden, setHidden] = useState<ReadonlySet<string>>(new Set());
  const [size, setSize] = useState({ width: 0, height: 0 });

  // Fetch + parse. Runs only in the browser: this component is lazily
  // imported from a click handler, so it never executes during prerender.
  useEffect(() => {
    const controller = new AbortController();
    let cancelled = false;
    setState({ status: "loading" });
    setView(null);
    fetch(fileUrl, { signal: controller.signal })
      .then(async (response) => {
        if (!response.ok) {
          throw new Error(`${response.status} ${response.statusText || "request failed"}`);
        }
        return response.arrayBuffer();
      })
      .then((buffer) => {
        if (cancelled) return;
        const data = flattenGds(parseGds(buffer));
        setState({ status: "ready", data });
      })
      .catch((error: unknown) => {
        if (cancelled || (error instanceof DOMException && error.name === "AbortError")) return;
        setState({
          status: "error",
          message: error instanceof Error ? error.message : "failed to load layout",
        });
      });
    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [fileUrl]);

  // Track the drawing surface's CSS size (canvas needs explicit pixels).
  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    const measure = () => {
      const rect = container.getBoundingClientRect();
      setSize({ width: Math.round(rect.width), height: Math.round(rect.height) });
    };
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(container);
    return () => observer.disconnect();
  }, [state.status]);

  const data = state.status === "ready" ? state.data : null;

  const styles = useMemo(() => {
    const map = new Map<string, LayerStyle>();
    for (const layer of data?.layers ?? []) {
      map.set(layer.key, resolveLayerStyle(layer.layer, layer.datatype, pdkFamily, layerNames));
    }
    return map;
  }, [data, pdkFamily, layerNames]);

  // Fit the layout to the surface once, as soon as BOTH the layout and a
  // non-zero surface measurement exist. The size check is load-bearing: the
  // measuring effect above and this one run in the same commit's effect
  // flush, so on the first ready render `size` is still `{0, 0}` here —
  // fitting against that would lock in a meaningless scale of 1 that no
  // later resize would correct (the guard below deliberately never clobbers
  // an existing view, so a user's pan/zoom survives re-renders).
  useEffect(() => {
    if (!data || size.width === 0 || size.height === 0) return;
    setView((previous) => previous ?? fitView(data.bbox, size.width, size.height));
  }, [data, size.width, size.height]);

  const resetView = useCallback(() => {
    if (!data) return;
    setView(fitView(data.bbox, size.width, size.height));
  }, [data, size.width, size.height]);

  const zoomBy = useCallback(
    (factor: number, anchorX?: number, anchorY?: number) => {
      setView((previous) =>
        previous
          ? zoomView(
              previous,
              factor,
              anchorX ?? size.width / 2,
              anchorY ?? size.height / 2,
              MIN_SCALE,
              MAX_SCALE,
            )
          : previous,
      );
    },
    [size.width, size.height],
  );

  // Draw on every view/visibility change.
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || !data || !view || size.width === 0 || size.height === 0) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    const dpr = typeof window !== "undefined" ? Math.min(window.devicePixelRatio || 1, 2) : 1;
    canvas.width = Math.max(1, Math.round(size.width * dpr));
    canvas.height = Math.max(1, Math.round(size.height * dpr));
    const visible = new Set(
      data.layers.map((layer) => layer.key).filter((key) => !hidden.has(key)),
    );
    drawLayout(ctx, data, view, visible, styles, size.width, size.height, dpr);
  }, [data, view, hidden, styles, size.width, size.height]);

  function handleWheel(event: ReactWheelEvent<HTMLDivElement>) {
    if (!view) return;
    event.preventDefault();
    const rect = event.currentTarget.getBoundingClientRect();
    zoomBy(
      event.deltaY < 0 ? ZOOM_STEP : 1 / ZOOM_STEP,
      event.clientX - rect.left,
      event.clientY - rect.top,
    );
  }

  function handlePointerDown(event: ReactPointerEvent<HTMLDivElement>) {
    dragRef.current = { x: event.clientX, y: event.clientY };
    event.currentTarget.setPointerCapture?.(event.pointerId);
  }

  function handlePointerMove(event: ReactPointerEvent<HTMLDivElement>) {
    const drag = dragRef.current;
    if (!drag) return;
    const dx = event.clientX - drag.x;
    const dy = event.clientY - drag.y;
    dragRef.current = { x: event.clientX, y: event.clientY };
    setView((previous) => (previous ? panView(previous, dx, dy) : previous));
  }

  function handlePointerUp(event: ReactPointerEvent<HTMLDivElement>) {
    dragRef.current = null;
    event.currentTarget.releasePointerCapture?.(event.pointerId);
  }

  function toggleLayer(key: string) {
    setHidden((previous) => {
      const next = new Set(previous);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }

  if (state.status === "loading") {
    return (
      <p data-testid="gds-canvas-status" className="m-auto font-mono text-[0.85rem] text-fog-dim">
        Loading {displayName} layout…
      </p>
    );
  }

  if (state.status === "error") {
    return (
      <div data-testid="gds-canvas-error" className="m-auto max-w-md text-center">
        <p className="font-mono text-[0.85rem] text-orange">
          Could not render this layout in the browser: {state.message}
        </p>
        <p className="mt-2 text-[0.8rem] text-fog-dim">
          The raw file is still available from the Downloads section.
        </p>
      </div>
    );
  }

  const layout = state.data;
  const bboxLabel = layout.bbox
    ? `${formatMicrons(layout.bbox.maxX - layout.bbox.minX, layout.dbuMicrons)} × ${formatMicrons(
        layout.bbox.maxY - layout.bbox.minY,
        layout.dbuMicrons,
      )} µm`
    : "empty";

  return (
    <div className="flex min-h-0 flex-1 gap-3">
      <div className="flex min-h-0 min-w-0 flex-1 flex-col gap-2">
        <div className="flex flex-wrap items-center gap-2">
          <button
            type="button"
            onClick={() => zoomBy(ZOOM_STEP)}
            className="rounded-lg border border-border bg-panel px-[0.7rem] py-[0.3rem] font-mono text-[0.8rem] text-fog hover:border-cyan focus-visible:border-cyan focus-visible:outline-none"
          >
            Zoom in
          </button>
          <button
            type="button"
            onClick={() => zoomBy(1 / ZOOM_STEP)}
            className="rounded-lg border border-border bg-panel px-[0.7rem] py-[0.3rem] font-mono text-[0.8rem] text-fog hover:border-cyan focus-visible:border-cyan focus-visible:outline-none"
          >
            Zoom out
          </button>
          <button
            type="button"
            onClick={resetView}
            className="rounded-lg border border-border bg-panel px-[0.7rem] py-[0.3rem] font-mono text-[0.8rem] text-fog hover:border-cyan focus-visible:border-cyan focus-visible:outline-none"
          >
            Fit
          </button>
          <p data-testid="gds-canvas-summary" className="font-mono text-[0.75rem] text-fog-dim">
            {layout.topName || "(unnamed top)"} · {layout.shapeCount.toLocaleString()} shapes ·{" "}
            {layout.layers.length} layers · {bboxLabel}
            {layout.truncated && " · truncated"}
          </p>
        </div>
        <div
          ref={containerRef}
          data-testid="gds-canvas-surface"
          className="min-h-0 flex-1 cursor-grab touch-none overflow-hidden rounded-lg border border-border bg-night active:cursor-grabbing"
          onWheel={handleWheel}
          onPointerDown={handlePointerDown}
          onPointerMove={handlePointerMove}
          onPointerUp={handlePointerUp}
          onPointerLeave={handlePointerUp}
        >
          <canvas
            ref={canvasRef}
            data-testid="gds-canvas"
            aria-label={`Rendered layout of ${displayName}`}
            role="img"
            style={{ width: "100%", height: "100%", display: "block" }}
          />
        </div>
        <p className="font-mono text-[0.72rem] text-fog-dim">
          Drag to pan · scroll to zoom · toggle layers on the right
        </p>
      </div>
      <div
        data-testid="gds-canvas-layers"
        aria-label="Layer visibility"
        className="flex w-[13rem] shrink-0 flex-col gap-1 overflow-y-auto rounded-lg border border-border bg-panel p-2"
      >
        <div className="flex gap-2 pb-1">
          <button
            type="button"
            onClick={() => setHidden(new Set())}
            className="font-mono text-[0.72rem] text-cyan hover:underline"
          >
            All
          </button>
          <button
            type="button"
            onClick={() => setHidden(new Set(layout.layers.map((layer) => layer.key)))}
            className="font-mono text-[0.72rem] text-cyan hover:underline"
          >
            None
          </button>
        </div>
        {layout.layers.map((layer) => {
          const style = styles.get(layer.key);
          return (
            <label
              key={layer.key}
              className="flex cursor-pointer items-center gap-2 font-mono text-[0.72rem] text-fog"
            >
              <input
                type="checkbox"
                checked={!hidden.has(layer.key)}
                onChange={() => toggleLayer(layer.key)}
              />
              <span
                aria-hidden="true"
                className="inline-block h-3 w-3 shrink-0 rounded-[2px] border border-border"
                style={{ backgroundColor: style?.fill ?? "#888" }}
              />
              <span className="truncate" title={`${style?.name ?? layer.key} (${layer.key})`}>
                {style?.name ?? layer.key}
              </span>
            </label>
          );
        })}
      </div>
    </div>
  );
}
