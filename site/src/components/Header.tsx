/**
 * Header — shared site chrome for the klayout-tools.org gallery (ported
 * from `Header.astro` in issue #92's Astro -> React migration; originally
 * issue #66).
 *
 * Rendered at the top of every page. Carries a "View on GitHub" link back
 * to the project repo. Uses the same `--color-*` Tailwind theme tokens
 * (`src/index.css`) the old component's CSS custom properties defined, so
 * the dark theme carries over unchanged.
 */
const REPO_URL = "https://github.com/2AMLogic/klayout-tools";

export function Header() {
  return (
    <header className="mx-auto flex w-full max-w-[44rem] items-center justify-between gap-4 border-b border-border px-6 py-4">
      <a
        href="/"
        className="font-mono text-[0.95rem] font-semibold text-fog no-underline hover:underline focus-visible:underline focus-visible:outline-none"
      >
        klayout-tools
      </a>
      <a
        href={REPO_URL}
        target="_blank"
        rel="noopener noreferrer"
        className="text-sm text-cyan no-underline hover:underline focus-visible:underline focus-visible:outline-none"
      >
        View on GitHub
      </a>
    </header>
  );
}
