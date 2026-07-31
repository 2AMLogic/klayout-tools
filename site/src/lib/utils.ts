import { type ClassValue, clsx } from "clsx";
import { twMerge } from "tailwind-merge";

/** shadcn/ui's standard `cn` helper: merges conditional class lists and
 * resolves conflicting Tailwind utility classes (last one wins). */
export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}
