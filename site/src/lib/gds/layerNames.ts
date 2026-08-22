/**
 * Layer identity helpers that carry no PDK color data (issue #943 / #1284).
 *
 * Deliberately separate from `layerStyle.ts`: the detail page itself needs
 * `PdkFamily` and `layerNamesFromRenders()` at first paint, while the
 * generated PDK color tables (~27 KB) must stay inside the viewer's lazy
 * chunk. Importing them from one module would drag those tables into the
 * page's initial bundle.
 */

/** PDK families the gallery styles layers for. */
export type PdkFamily = "sky130" | "gf180mcu" | "ihp-sg13g2";

/**
 * Derives `{ "<layer>/<datatype>": "<name>" }` from a block's `renders` map.
 *
 * The pipeline keys per-layer renders by PDK layer name and stores them at
 * `renders/<layer>_<datatype>.png` (see `scripts/_gallery_common.py`), so
 * the file name carries the numbers and the key carries the name — which is
 * exactly the mapping the viewer's layer list wants, per block, with no PDK
 * guess involved. Synthetic ids (`overview`, `center_crop`) and the
 * `layer_<n>_<n>` numeric fallback keys contribute nothing and are skipped.
 */
export function layerNamesFromRenders(
  renders: Record<string, string> | undefined,
): Record<string, string> {
  const out: Record<string, string> = {};
  if (!renders) return out;
  for (const [name, path] of Object.entries(renders)) {
    if (name === "overview" || name === "center_crop") continue;
    if (/^layer_-?\d+_-?\d+$/.test(name)) continue;
    const match = /(-?\d+)_(-?\d+)\.png$/.exec(path);
    if (!match) continue;
    out[`${Number(match[1])}/${Number(match[2])}`] = name;
  }
  return out;
}
