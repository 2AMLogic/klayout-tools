import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/utils";

/**
 * shadcn/ui-style `Badge` primitive, themed for the dark gallery palette
 * (see `src/index.css`'s `@theme` block). Used for the LayoutCard status
 * chip and metric badges (issue #92 — replaces the Astro `.chip`/`.badge`
 * inline styles with reusable variants).
 */
const badgeVariants = cva(
  "inline-flex items-center gap-1 rounded-full border px-2.5 py-0.5 text-xs font-semibold tracking-wide transition-colors",
  {
    variants: {
      variant: {
        default: "border-border bg-white/[0.04] text-fog",
        outline: "border-fog-dim/60 text-fog-dim",
        ok: "border-transparent bg-[#3fe082]/90 text-[#04150d]",
        partial: "border-transparent bg-orange/90 text-[#1a1300]",
        "no-artifacts": "border-transparent bg-fog-dim/85 text-[#0c1116]",
        warn: "border-orange/60 bg-orange/10 text-fog",
      },
    },
    defaultVariants: {
      variant: "default",
    },
  },
);

export interface BadgeProps
  extends React.HTMLAttributes<HTMLSpanElement>,
    VariantProps<typeof badgeVariants> {}

function Badge({ className, variant, ...props }: BadgeProps) {
  return <span className={cn(badgeVariants({ variant }), className)} {...props} />;
}

export { Badge, badgeVariants };
