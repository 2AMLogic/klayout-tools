import { Suspense, lazy, useEffect } from "react";
import { createPortal } from "react-dom";
import type { PdkFamily } from "@/lib/gds/layerNames";

/**
 * Embedded, same-page GDS viewer overlay for a block detail page
 * (issue #943, rendering engine completed by #1284).
 *
 * Before this component, the only interactive way to inspect a block's
 * actual layout was a "View in browser" link buried in the Downloads
 * section that navigated away to a third-party hosted viewer in a new tab --
 * nothing on the page itself was clickable, and the render thumbnails (the
 * natural affordance) were static `<figure>` images. `DetailPage.tsx` now
 * opens this component as an in-page overlay from both the render thumbnails
 * and the Downloads section's viewer entry, so a visitor never leaves
 * klayout-tools.org to zoom/pan/toggle layers on a block's GDS.
 *
 * Three implementation paths were on the table in issue #943: (a) vendor a
 * third-party WASM parser + WebGL renderer, (b) precompute glTF at ingest
 * time and adopt Tiny Tapeout's three.js viewer, or (c) embed Tiny Tapeout's
 * *hosted* viewer in an `<iframe>`. (c) shipped first as a stopgap; this is
 * the follow-through (#1284): a small, dependency-free GDSII reader plus a
 * 2D-canvas renderer written here (`@/lib/gds` + `GdsCanvas.tsx`). It keeps
 * (a)'s "no pipeline artifact" property and (c)'s "no new npm/WASM
 * dependency" property while removing the third-party runtime dependency
 * both (b) and (c) carry -- the raw GDS is already staged same-origin by
 * `copy-renders.mjs`, so nothing but the browser is needed to draw it.
 *
 * The renderer is `lazy()`-loaded: the parser, flattener, and the generated
 * PDK color tables are a separate chunk that a detail page only downloads
 * once a visitor actually opens the viewer.
 *
 * SSR-safe the same way `FieldViewer`/`WaveformViewer` are: `useEffect` never
 * runs during `renderToString` (`entry-server.tsx`'s prerender), and
 * `document`/`window` are only ever touched from inside a `useEffect` or a
 * DOM event handler, never at module-eval or render time -- `isOpen` starts
 * `false` and can only flip via a client-side click, so `createPortal`'s
 * `document.body` reference (and the lazy chunk's own import) is never
 * evaluated during SSR.
 */
const GdsCanvas = lazy(() => import("./GdsCanvas"));

export interface GdsViewerProps {
  /** Whether the overlay is currently shown. */
  isOpen: boolean;
  /** Invoked on close (Escape key, backdrop click, or the Close button). */
  onClose: () => void;
  /** Same-origin URL to the block's staged GDS file. */
  fileUrl: string;
  /**
   * PDK family used to color layers the way KLayout would. Omitted when the
   * block's PDK family couldn't be inferred -- the renderer then falls back
   * to deterministic per-layer hues rather than styling with a wrong PDK.
   */
  pdkFamily?: PdkFamily;
  /**
   * `"<layer>/<datatype>" -> layer name` overrides derived from the block's
   * own `renders` map, so the viewer's layer list is worded exactly like the
   * per-layer thumbnail captions on the page behind it.
   */
  layerNames?: Record<string, string>;
  /** Block display name, used for the overlay's heading/aria-label. */
  displayName: string;
}

export function GdsViewer({
  isOpen,
  onClose,
  fileUrl,
  pdkFamily,
  layerNames,
  displayName,
}: GdsViewerProps) {
  useEffect(() => {
    if (!isOpen) return;
    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") onClose();
    }
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [isOpen, onClose]);

  if (!isOpen) return null;

  return createPortal(
    <div
      role="dialog"
      aria-modal="true"
      aria-label={`Interactive GDS viewer for ${displayName}`}
      className="fixed inset-0 z-50 flex flex-col gap-3 bg-night/95 p-4"
      onClick={onClose}
    >
      <div
        className="flex items-center justify-between"
        onClick={(event) => event.stopPropagation()}
      >
        <p className="font-mono text-[0.9rem] text-fog">{displayName} — GDS viewer</p>
        <button
          type="button"
          onClick={onClose}
          className="rounded-lg border border-border bg-panel px-[0.9rem] py-[0.4rem] text-[0.85rem] text-fog hover:border-cyan focus-visible:border-cyan focus-visible:outline-none"
        >
          Close
        </button>
      </div>
      <div
        className="flex min-h-0 flex-1 flex-col"
        onClick={(event) => event.stopPropagation()}
      >
        <Suspense
          fallback={
            <p className="m-auto font-mono text-[0.85rem] text-fog-dim">Loading viewer…</p>
          }
        >
          <GdsCanvas
            fileUrl={fileUrl}
            displayName={displayName}
            pdkFamily={pdkFamily}
            layerNames={layerNames}
          />
        </Suspense>
      </div>
    </div>,
    document.body,
  );
}
