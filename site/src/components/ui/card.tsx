import * as React from "react";
import { cn } from "@/lib/utils";

/**
 * shadcn/ui-style `Card` primitives, themed for the dark gallery palette.
 * `LayoutCard` (issue #92) composes these instead of Astro's bespoke
 * `.card` styles.
 */
function Card({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cn(
        "flex flex-col overflow-hidden rounded-xl border border-border bg-panel text-fog transition-[transform,box-shadow,border-color] duration-150",
        className,
      )}
      {...props}
    />
  );
}

function CardContent({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("flex flex-col gap-2 px-4 pt-3.5 pb-4", className)} {...props} />;
}

function CardTitle({ className, ...props }: React.HTMLAttributes<HTMLHeadingElement>) {
  return (
    <h2
      className={cn("font-mono text-[1.05rem] leading-tight break-words text-fog", className)}
      {...props}
    />
  );
}

function CardDescription({ className, ...props }: React.HTMLAttributes<HTMLParagraphElement>) {
  return (
    <p
      className={cn(
        "line-clamp-3 text-sm leading-snug text-fog-dim [display:-webkit-box] [-webkit-box-orient:vertical]",
        className,
      )}
      {...props}
    />
  );
}

export { Card, CardContent, CardTitle, CardDescription };
