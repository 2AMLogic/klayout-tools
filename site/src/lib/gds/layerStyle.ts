/**
 * Per-layer display styling for the embedded GDS viewer (issue #943 /
 * #1284).
 *
 * Colors and layer names come from the PDK's own KLayout layer-property
 * file, extracted into `layerStyles.generated.ts` by
 * `scripts/gen-gds-layer-styles.mjs`, so a block rendered in the browser is
 * styled the same way KLayout styles it when the Python pipeline renders
 * that block's per-layer PNGs. A layer the PDK table doesn't cover (or a
 * block whose PDK family couldn't be inferred) falls back to a deterministic
 * hue derived from its layer/datatype numbers — a stable, distinguishable
 * color rather than a wrong PDK's color.
 */
import { GENERATED_LAYER_STYLES, type GeneratedLayerStyle } from "./layerStyles.generated";
import type { PdkFamily } from "./layerNames";

export interface LayerStyle {
  /** Fill color, `#rrggbb`. */
  fill: string;
  /** Outline color, `#rrggbb`. */
  frame: string;
  /** Human-readable layer name, e.g. `"met1.drawing"` / `"Metal1"`. */
  name: string;
  /** True when `fill`/`frame` came from the PDK table, not the fallback. */
  fromPdk: boolean;
}

const TABLE_CACHE = new Map<string, Map<string, GeneratedLayerStyle>>();

function tableFor(family: PdkFamily | undefined): Map<string, GeneratedLayerStyle> | undefined {
  if (!family) return undefined;
  const cached = TABLE_CACHE.get(family);
  if (cached) return cached;
  const rows = GENERATED_LAYER_STYLES[family];
  if (!rows) return undefined;
  const table = new Map<string, GeneratedLayerStyle>();
  for (const row of rows) {
    const key = `${row[0]}/${row[1]}`;
    // First entry wins: KLayout lists the primary (drawing) view first.
    if (!table.has(key)) table.set(key, row);
  }
  TABLE_CACHE.set(family, table);
  return table;
}

/**
 * Deterministic fallback color for a layer the PDK table doesn't cover.
 * Uses the golden-angle hue sequence so numerically adjacent layers get
 * visually distinct colors instead of near-identical neighbours.
 */
export function fallbackLayerColor(layer: number, datatype: number): string {
  const index = Math.abs(layer * 64 + datatype);
  const hue = (index * 137.508) % 360;
  return hslToHex(hue, 0.62, 0.58);
}

function hslToHex(h: number, s: number, l: number): string {
  const c = (1 - Math.abs(2 * l - 1)) * s;
  const x = c * (1 - Math.abs(((h / 60) % 2) - 1));
  const m = l - c / 2;
  const [r, g, b] =
    h < 60
      ? [c, x, 0]
      : h < 120
        ? [x, c, 0]
        : h < 180
          ? [0, c, x]
          : h < 240
            ? [0, x, c]
            : h < 300
              ? [x, 0, c]
              : [c, 0, x];
  const to255 = (v: number) =>
    Math.round((v + m) * 255)
      .toString(16)
      .padStart(2, "0");
  return `#${to255(r)}${to255(g)}${to255(b)}`;
}

/**
 * Resolves the style for one `(layer, datatype)` pair.
 *
 * `nameOverrides` (keyed `"<layer>/<datatype>"`) takes precedence over the
 * PDK table's name — the content pipeline already labels each per-layer
 * render with the same PDK layer name, so passing those labels through keeps
 * the viewer's layer list worded identically to the thumbnail captions on
 * the page behind it.
 */
export function resolveLayerStyle(
  layer: number,
  datatype: number,
  family?: PdkFamily,
  nameOverrides?: Record<string, string>,
): LayerStyle {
  const key = `${layer}/${datatype}`;
  const row = tableFor(family)?.get(key);
  const overrideName = nameOverrides?.[key];
  if (row) {
    return {
      fill: row[2],
      frame: row[3],
      name: overrideName || row[4] || `Layer ${key}`,
      fromPdk: true,
    };
  }
  const color = fallbackLayerColor(layer, datatype);
  return {
    fill: color,
    frame: color,
    name: overrideName || `Layer ${key}`,
    fromPdk: false,
  };
}
