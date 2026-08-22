import { useEffect } from "react";
import { createPortal } from "react-dom";

/**
 * Embedded, same-page GDS/OAS viewer overlay for a block detail page
 * (issue #943).
 *
 * Before this component, the only interactive way to inspect a block's
 * actual layout was a "View in browser" link buried in the Downloads
 * section that navigated away to Tiny Tapeout's hosted viewer
 * (`gds-viewer.tinytapeout.com`) in a new tab -- nothing on the page itself
 * was clickable, and the render thumbnails (the natural affordance) were
 * static `<figure>` images. `DetailPage.tsx` now opens this component as an
 * in-page overlay from both the render thumbnails and the Downloads
 * section's viewer entry, so a visitor never leaves klayout-tools.org to
 * zoom/pan/toggle layers on a block's GDS.
 *
 * Implementation choice (documented here since three genuinely different
 * paths were on the table -- see issue #943's "Proposed work" item 1 and
 * the Champion review requesting one be picked with rationale):
 *
 *   a. vendor `znah/tiny_explorer`'s WASM parser + WebGL renderer, parsing
 *      the raw GDS fully client-side;
 *   b. adopt Tiny Tapeout's own viewer + precompute glTF via GDS2glTF at
 *      ingest time, adding a new pipeline artifact per block;
 *   c. keep Tiny Tapeout's hosted viewer as the rendering engine, but embed
 *      it in-page (an `<iframe>` overlay opened by a click, never a
 *      navigation) instead of linking out to a new tab, and stop guessing
 *      at (or hardcoding) its `pdk` query param.
 *
 * This component takes (c). It is the cheapest-workable option with zero
 * client bundle cost -- an `<iframe>` has no JS to ship, unlike (a)'s
 * vendored WASM/WebGL engine or (b)'s new build-time glTF conversion step
 * -- and it directly resolves this issue's actual discoverability gap
 * (nothing was clickable) without committing to a heavier, harder-to-review
 * architecture change in the same PR. It does not resolve the third-party
 * hosted-service dependency the issue's "gap" section also calls out --
 * that trade-off is deliberate, not an oversight, and is tracked as
 * follow-up scope for (a)/(b) rather than solved here (the issue itself
 * frames (c) as an acceptable "stopgap" path, not a rejected one). Follow-up:
 * issue #1284 (same-origin rendering engine, options (a)/(b) above) and
 * issue #1285 (explicit `pdk` field on `layout.json`, replacing the
 * slug-prefix heuristic `resolveViewerPdk()` in `DetailPage.tsx` still uses
 * to fill this component's `pdk` prop).
 *
 * SSR-safe the same way `FieldViewer`/`WaveformViewer` are: `useEffect`
 * never runs during `renderToString` (`entry-server.tsx`'s prerender), and
 * `document`/`window` are only ever touched from inside a `useEffect` or a
 * DOM event handler, never at module-eval or render time -- `isOpen` starts
 * `false` and can only flip via a client-side click, so `createPortal`'s
 * `document.body` reference is never evaluated during SSR.
 */
export interface GdsViewerProps {
  /** Whether the overlay is currently shown. */
  isOpen: boolean;
  /** Invoked on close (Escape key, backdrop click, or the Close button). */
  onClose: () => void;
  /** Same-origin, absolute URL to the block's staged GDS/OAS file. */
  fileUrl: string;
  /**
   * Tiny Tapeout viewer PDK identifier (e.g. `"sky130A"`, `"gf180mcuD"`).
   * Omitted when the block's PDK family couldn't be inferred -- the viewer
   * falls back to its own default rather than being told a wrong PDK.
   */
  pdk?: string;
  /** Block display name, used for the overlay's heading/aria-label. */
  displayName: string;
}

/**
 * Builds a Tiny Tapeout hosted-viewer URL for a block's layout file,
 * embedded via `<iframe src>` rather than opened as a link (issue #943;
 * originally issue #249's `gdsViewerUrl()`, moved here since the viewer URL
 * is now this component's own concern). Mirrors Tiny Tapeout's own
 * `?model=<url>&pdk=<pdk>` convention (github.com/TinyTapeout/tt-gds-action).
 */
export function buildViewerSrc(fileUrl: string, pdk?: string): string {
  const params = new URLSearchParams({ model: fileUrl });
  if (pdk) {
    params.set("pdk", pdk);
  }
  return `https://gds-viewer.tinytapeout.com/?${params.toString()}`;
}

export function GdsViewer({ isOpen, onClose, fileUrl, pdk, displayName }: GdsViewerProps) {
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
      <iframe
        title={`Interactive GDS viewer for ${displayName}`}
        src={buildViewerSrc(fileUrl, pdk)}
        className="min-h-0 flex-1 rounded-lg border border-border bg-night"
        onClick={(event) => event.stopPropagation()}
      />
    </div>,
    document.body,
  );
}
