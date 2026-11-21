/**
 * Header — shared site chrome for the klayout-tools.org gallery (ported
 * from `Header.astro` in issue #92's Astro -> React migration; originally
 * issue #66; in-page section nav added in issue #4597).
 *
 * Rendered at the top of the index page. Carries in-page links to the
 * landing page's sections and a "GitHub" link back to the project repo.
 * Uses the same `--color-*` Tailwind theme tokens (`src/index.css`) the old
 * component's CSS custom properties defined, so the dark theme carries over
 * unchanged.
 */
const REPO_URL = "https://github.com/2AMLogic/klayout-tools";

const NAV = [
  { href: "/#quick-start", label: "Quick start" },
  { href: "/#capabilities", label: "Capabilities" },
  { href: "/#gallery", label: "Gallery" },
  { href: "/#docs", label: "Docs" },
];

const link =
  "text-sm no-underline hover:underline focus-visible:underline focus-visible:outline-none";

export function Header() {
  return (
    <header className="mx-auto flex w-full max-w-[72rem] flex-wrap items-center justify-between gap-x-6 gap-y-2 border-b border-border px-6 py-4">
      <a
        href="/"
        className="font-mono text-[0.95rem] font-semibold text-fog no-underline hover:underline focus-visible:underline focus-visible:outline-none"
      >
        klayout-tools
      </a>
      <nav aria-label="Sections" className="flex flex-wrap items-center gap-x-5 gap-y-1">
        {NAV.map((item) => (
          <a key={item.href} href={item.href} className={`${link} text-fog-dim hover:text-cyan`}>
            {item.label}
          </a>
        ))}
        <a href={REPO_URL} target="_blank" rel="noopener noreferrer" className={`${link} text-cyan`}>
          GitHub
        </a>
      </nav>
    </header>
  );
}
