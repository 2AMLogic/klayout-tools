import type { Layout, LayoutStatus } from "@/data/types";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardTitle, CardDescription } from "@/components/ui/card";

/**
 * LayoutCard — one gallery card per discovered block (ported from
 * `LayoutCard.astro` in issue #92's Astro -> React migration; originally
 * issue #63, mirroring kicad-tools#3680/#3683's BoardCard).
 *
 * Renders a thumbnail (preferring the composite `overview` render, else the
 * first `renders` entry, falling back to a static placeholder), the block
 * `name` + `description`, a status chip driven by `Layout.status`, and a set
 * of metric badges. The status chip is honest: it is omitted entirely for
 * `"ok"` blocks (no chip oversells a plain, ready block) and rendered only
 * for `"partial"` / `"no_artifacts"`, which carry actionable meaning. Every
 * optional field follows
 * the data contract's omit-when-absent rule: a badge is rendered only when
 * its backing field is `!== undefined`, never as "null" or "—" (see
 * `src/data/types.ts`).
 *
 * The whole card links to `/<slug>/` — the per-block detail route.
 */
export interface LayoutCardProps {
  layout: Layout;
}

/**
 * Status-chip labels. `"ok"` deliberately has no entry: a ready block gets
 * no chip at all (see `thumbnailFor` docblock and the render below), so only
 * the two statuses that carry actionable meaning are mapped here.
 */
const STATUS_LABEL: Record<Exclude<LayoutStatus, "ok">, string> = {
  partial: "Partial",
  no_artifacts: "No artifacts",
  "in design — simulation evidence": "In design",
};

const STATUS_BADGE_VARIANT: Record<
  Exclude<LayoutStatus, "ok">,
  "partial" | "no-artifacts" | "in-design"
> = {
  partial: "partial",
  no_artifacts: "no-artifacts",
  "in design — simulation evidence": "in-design",
};

/**
 * Resolve the thumbnail URL. `layout.renders` maps a render id to a path
 * relative to the block's `output/` directory (e.g. `"renders/1_0.png"`,
 * per `docs/cli/layout-metrics.md`); the `copy-renders` prebuild step
 * stages that same tree at `site/public/blocks/<slug>/`, so the value can
 * be appended to `/blocks/<slug>/` verbatim.
 *
 * When the block ships a composite `"overview"` render (a single image
 * showing every layer together, produced by `klt render`), prefer it — a
 * per-layer render picked by object-insertion order is a poor,
 * unrepresentative thumbnail. Fall back to the first `renders` entry, then
 * to the static placeholder.
 */
function thumbnailFor(l: Layout): { src: string; isPlaceholder: boolean } {
  const renders = l.renders;
  const firstEntry = renders ? renders["overview"] ?? Object.values(renders)[0] : undefined;
  if (firstEntry) {
    const rel = firstEntry.replace(/^\.?\//, "");
    return { src: `/blocks/${l.slug}/${rel}`, isPlaceholder: false };
  }
  return { src: "/placeholder-layout.svg", isPlaceholder: true };
}

export function LayoutCard({ layout }: LayoutCardProps) {
  const thumb = thumbnailFor(layout);
  const displayName = layout.name || layout.slug;
  const href = `/${layout.slug}/`;
  // Honest status chip: `"ok"` blocks get no chip at all — only the two
  // statuses that flag an incomplete block render a Badge. Narrowing `status`
  // to a non-`"ok"` value here also keeps the `STATUS_*` record lookups
  // below type-safe (those records intentionally omit the `"ok"` key).
  const status = layout.status;

  return (
    <a
      href={href}
      data-status={layout.status}
      className="block no-underline focus-visible:outline-none"
    >
      <Card className="h-full hover:-translate-y-[3px] hover:border-cyan hover:shadow-[0_8px_24px_rgba(0,0,0,0.35)] focus-within:-translate-y-[3px] focus-within:border-cyan">
        <div className="relative aspect-4/3 overflow-hidden bg-night">
          <img
            src={thumb.src}
            alt={
              thumb.isPlaceholder
                ? `No render available for ${displayName}`
                : `Render of ${displayName}`
            }
            loading="lazy"
            decoding="async"
            className={
              thumb.isPlaceholder
                ? "block h-full w-full object-cover"
                : "block h-full w-full object-contain"
            }
          />
          {status !== "ok" && (
            <Badge
              variant={STATUS_BADGE_VARIANT[status]}
              className="absolute top-2.5 right-2.5 backdrop-blur-sm"
            >
              {STATUS_LABEL[status]}
            </Badge>
          )}
        </div>

        <CardContent>
          <CardTitle>{displayName}</CardTitle>
          {layout.description && <CardDescription>{layout.description}</CardDescription>}

          <ul className="mt-1 flex flex-wrap gap-1.5">
            {layout.layer_count !== undefined && (
              <li>
                <Badge className="gap-1.5">
                  <span className="text-[0.64rem] tracking-wide text-fog-dim uppercase">
                    Layers
                  </span>
                  <span className="font-semibold text-fog">{layout.layer_count}</span>
                </Badge>
              </li>
            )}
            {layout.cell_count !== undefined && (
              <li>
                <Badge className="gap-1.5">
                  <span className="text-[0.64rem] tracking-wide text-fog-dim uppercase">
                    Cells
                  </span>
                  <span className="font-semibold text-fog">{layout.cell_count}</span>
                </Badge>
              </li>
            )}
            {layout.instance_count !== undefined && (
              <li>
                <Badge className="gap-1.5">
                  <span className="text-[0.64rem] tracking-wide text-fog-dim uppercase">
                    Instances
                  </span>
                  <span className="font-semibold text-fog">{layout.instance_count}</span>
                </Badge>
              </li>
            )}
            {layout.drc !== undefined && (
              <li>
                <Badge variant={layout.drc.violation_count > 0 ? "warn" : "default"} className="gap-1.5">
                  <span className="text-[0.64rem] tracking-wide text-fog-dim uppercase">DRC</span>
                  <span className="font-semibold text-fog">
                    {layout.drc.status === "clean"
                      ? "clean"
                      : `${layout.drc.violation_count} violation${layout.drc.violation_count === 1 ? "" : "s"}`}
                  </span>
                </Badge>
              </li>
            )}
          </ul>
        </CardContent>
      </Card>
    </a>
  );
}
