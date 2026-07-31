/**
 * Shared helper for resolving block-relative artifact paths (renders,
 * downloadable layout files, and — as of issue #100 — signals/waveform
 * JSON) to the URLs `copy-renders.mjs` stages them at.
 *
 * Extracted from `DetailPage.tsx` (originally issue #64) so
 * `components/waveform/WaveformViewer.tsx` can resolve the same
 * `output/`-relative paths without duplicating the convention.
 */

/**
 * Served URL for a path relative to a block's `output/` directory (the
 * convention `renders`/`layout_file`/`signals[].corners[].waveform` values
 * use — see `data/types.ts`), staged by `copy-renders.mjs` into
 * `public/blocks/<slug>/...`.
 */
export function blockAssetUrl(slug: string, relPath: string): string {
  return `/blocks/${slug}/${relPath.replace(/^\.?\//, "")}`;
}
