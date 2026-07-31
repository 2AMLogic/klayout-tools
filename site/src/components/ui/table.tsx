import * as React from "react";
import { cn } from "@/lib/utils";

/**
 * shadcn/ui-style `Table` primitives, themed for the dark gallery palette.
 * Used by the block detail page's metrics table (issue #92 — replaces the
 * Astro `<table>`'s inline `<style>` block).
 */
function Table({ className, ...props }: React.HTMLAttributes<HTMLTableElement>) {
  return (
    <div className="overflow-hidden rounded-lg border border-border">
      <table className={cn("w-full border-collapse text-sm", className)} {...props} />
    </div>
  );
}

function TableBody({ className, ...props }: React.HTMLAttributes<HTMLTableSectionElement>) {
  return <tbody className={cn(className)} {...props} />;
}

function TableRow({ className, ...props }: React.HTMLAttributes<HTMLTableRowElement>) {
  return (
    <tr
      className={cn("border-b border-border last:border-b-0", className)}
      {...props}
    />
  );
}

function TableHead({ className, ...props }: React.ThHTMLAttributes<HTMLTableCellElement>) {
  return (
    <th
      scope="row"
      className={cn(
        "w-2/5 bg-panel px-3.5 py-2.5 text-left font-medium text-fog-dim",
        className,
      )}
      {...props}
    />
  );
}

function TableCell({ className, ...props }: React.TdHTMLAttributes<HTMLTableCellElement>) {
  return (
    <td className={cn("px-3.5 py-2.5 text-left font-mono text-fog", className)} {...props} />
  );
}

export { Table, TableBody, TableRow, TableHead, TableCell };
