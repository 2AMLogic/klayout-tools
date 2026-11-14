import type { Layout } from "@/data/types";

/**
 * Gallery grouping helpers for `IndexPage` (issue #4642): which PDK a block
 * targets, whether it is a canary block, and the filter predicates.
 */

export type PdkFilter = "all" | string;
export type StatusFilter = "all" | "layout" | "partial" | "in-design" | "no-artifacts";

/** Display order for known PDK families; unknown ones sort after, by name. */
const PDK_ORDER = ["gf180mcu", "sky130", "sg13g2", "sg13cmos5l", "asap7"];

/**
 * `layout.pdk` when present (issue #1285), else the `<pdk>-<name>` slug
 * prefix (canary repos) or `<pdk>_fd_sc_...` prefix (corpus cells); `undefined`
 * when neither says. `sg13cmos5l` and `asap7` have no `klt` deck, so their
 * blocks never carry `layout.pdk` and rely on the slug.
 */
export function pdkOf(l: Layout): string | undefined {
  if (l.pdk) return l.pdk;
  const m = /^(gf180mcu|gf180|sky130|sg13cmos5l|sg13g2|asap7)[-_]/.exec(l.slug);
  if (!m) return undefined;
  return m[1] === "gf180" ? "gf180mcu" : m[1];
}

/** Canary blocks carry `source` provenance (ingest pipeline); corpus cells do not. */
export function isCanary(l: Layout): boolean {
  return l.source !== undefined;
}

export function statusGroup(l: Layout): Exclude<StatusFilter, "all"> {
  if (l.status === "partial") return "partial";
  if (l.status === "no_artifacts") return "no-artifacts";
  if (l.status === "in design — simulation evidence") return "in-design";
  return "layout";
}

/** PDK families present among `layouts`, in display order. */
export function pdksIn(layouts: Layout[]): string[] {
  const set = new Set<string>();
  for (const l of layouts) {
    const p = pdkOf(l);
    if (p) set.add(p);
  }
  const rank = (p: string) => {
    const i = PDK_ORDER.indexOf(p);
    return i === -1 ? PDK_ORDER.length : i;
  };
  return [...set].sort((a, b) => rank(a) - rank(b) || a.localeCompare(b));
}

export function filterLayouts(
  layouts: Layout[],
  pdk: PdkFilter,
  status: StatusFilter,
): Layout[] {
  return layouts.filter(
    (l) =>
      (pdk === "all" || pdkOf(l) === pdk) &&
      (status === "all" || statusGroup(l) === status),
  );
}
