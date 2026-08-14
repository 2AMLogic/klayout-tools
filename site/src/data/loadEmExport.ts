/**
 * Build-time field-data (em-export) loader for a gallery block's detail page
 * (Epic #840 Phase 3b, issue #959).
 *
 * Reads `blocks/<slug>/output/<slug>.em-export.json` — the per-block
 * geode-fem coupling export produced by Phase 3a (issue #958, PR #972) —
 * when present, and returns it typed as `EmSiteExport` (the same shape
 * `site/src/components/em/types.ts` already declares for the landing-page EM
 * gallery's fetched exports; see that module's own doc comment for the
 * schema's `additionalProperties: true` looseness).
 *
 * Mirrors `loadLayouts.ts`'s resilience pattern *exactly* (see that module's
 * doc comment) rather than reusing it directly — an em-export artifact is
 * optional per block (most blocks have none), so this loader returns `null`
 * (no panel) instead of `loadLayouts`'s always-present stub:
 *   - Missing `<slug>.em-export.json` -> `null`, no panel, never throws.
 *   - Unreadable / unparsable JSON -> `console.warn` + `null`.
 *   - Unknown `schema_version` -> `console.warn` + `null` (forward-compat
 *     guard — a future schema bump does not need this loader touched to
 *     avoid crashing the build; it just stops rendering until the loader
 *     itself is updated to understand the new version).
 *   - Missing required top-level fields, or neither `s_parameters` nor
 *     `capacitance` present (the schema's own `anyOf`, see
 *     `components/em/types.ts`'s `EmSiteExport` doc comment) -> `null`.
 *
 * Deliberately NOT a reuse of the EM gallery's existing data-fetching
 * pattern (`SpiralInductorResult.tsx` et al. `fetch()` their JSON
 * client-side from `public/em/...`) — this is the `loadLayouts.ts`
 * build-time `fs`-read pattern instead, so the field panel is present in the
 * server-rendered HTML (no client-side loading flash, no extra request).
 */

import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import type { EmSiteExport } from "@/components/em/types";

/** `schema_version` this loader understands (`docs/schemas/em-site-export.schema.json`,
 *  issue #849). A document with any other value is skipped. */
const SUPPORTED_SCHEMA_VERSION = 1;

/**
 * Validate a parsed JSON value against the minimal shape `EmSiteExport`
 * requires. Returns the value typed as `EmSiteExport` when valid, or `null`
 * when it is missing required fields, has an unknown `schema_version`, or
 * satisfies neither half of the schema's `s_parameters`/`capacitance`
 * `anyOf`.
 *
 * Like `loadLayouts.ts`'s `validateLayout`, only required-field presence is
 * enforced; the schema's own `additionalProperties: true` on nested objects
 * means downstream consumers (`FieldViewer`, `ProvenancePanel`) may read
 * more fields than checked here.
 */
function validateEmExport(data: unknown, slug: string): EmSiteExport | null {
  if (typeof data !== "object" || data === null) {
    console.warn(`[loadEmExport] ${slug}: em-export.json is not an object; skipping panel`);
    return null;
  }
  const obj = data as Record<string, unknown>;

  if (obj.schema_version !== SUPPORTED_SCHEMA_VERSION) {
    console.warn(
      `[loadEmExport] ${slug}: unknown schema_version ` +
        `${JSON.stringify(obj.schema_version)} (expected ${SUPPORTED_SCHEMA_VERSION}); skipping panel`,
    );
    return null;
  }

  const required = ["benchmark", "mesh", "frames", "provenance"] as const;
  for (const key of required) {
    if (!(key in obj)) {
      console.warn(`[loadEmExport] ${slug}: em-export.json missing required field "${key}"; skipping panel`);
      return null;
    }
  }

  if (obj.s_parameters === undefined && obj.capacitance === undefined) {
    console.warn(
      `[loadEmExport] ${slug}: em-export.json has neither s_parameters nor capacitance; skipping panel`,
    );
    return null;
  }

  return obj as unknown as EmSiteExport;
}

/**
 * Read and validate a single block directory's `<slug>.em-export.json`.
 *
 * `blockPath` is a block directory (e.g. `<blocksDir>/gf180-bandgap`),
 * matching `loadLayout(blockPath)`'s own argument shape. Returns `null`
 * (never throws) whenever there is nothing to render — the caller renders
 * no panel in that case, matching a block with no em-export artifact today.
 */
export function loadEmExport(blockPath: string): EmSiteExport | null {
  const slug = blockPath.split(/[\\/]/).filter(Boolean).pop() ?? blockPath;
  const jsonPath = join(blockPath, "output", `${slug}.em-export.json`);

  if (!existsSync(jsonPath)) {
    return null;
  }

  let raw: string;
  try {
    raw = readFileSync(jsonPath, "utf8");
  } catch (err) {
    console.warn(`[loadEmExport] ${slug}: failed to read em-export.json (${String(err)}); skipping panel`);
    return null;
  }

  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch (err) {
    console.warn(`[loadEmExport] ${slug}: em-export.json is not valid JSON (${String(err)}); skipping panel`);
    return null;
  }

  return validateEmExport(parsed, slug);
}
