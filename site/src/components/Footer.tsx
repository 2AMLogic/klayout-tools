/**
 * Footer — shared site chrome for the klayout-tools.org gallery (ported
 * from `Footer.astro` in issue #92's Astro -> React migration; originally
 * issue #66).
 *
 * Carries provenance: the build-time short git commit hash (linked to the
 * commit on GitHub when resolvable), a copyright line, and a link to the
 * project's own scope doc.
 *
 * `getBuildSha`/`commitUrl` (`src/data/buildInfo.ts`) touch `node:child_process`
 * and must never be imported into client-bundled code — only
 * `src/entry-server.tsx` (Node-only) calls them, at prerender time, and
 * passes the resolved values down as plain props so this component stays
 * safe to hydrate in the browser.
 */
export interface FooterProps {
  repoUrl: string;
  buildSha: string;
  buildUrl: string;
  year: number;
}

export function Footer({ repoUrl, buildSha, buildUrl, year }: FooterProps) {
  return (
    <footer className="mx-auto w-full max-w-[44rem] border-t border-border px-6 py-6 pb-8 text-[0.8rem] leading-relaxed text-fog-dim">
      <p className="my-1.5">
        Build:{" "}
        <a
          href={buildUrl}
          target="_blank"
          rel="noopener noreferrer"
          className="text-fog-dim underline decoration-1 underline-offset-2 hover:text-cyan focus-visible:text-cyan focus-visible:outline-none"
        >
          <code className="font-mono">{buildSha}</code>
        </a>
      </p>
      <p className="my-1.5">© {year} 2AM Logic / klayout-tools contributors</p>
      <p className="my-1.5">
        <a
          href={`${repoUrl}/blob/main/docs/ARCHITECTURE.md`}
          target="_blank"
          rel="noopener noreferrer"
          className="text-fog-dim underline decoration-1 underline-offset-2 hover:text-cyan focus-visible:text-cyan focus-visible:outline-none"
        >
          Project scope &amp; architecture
        </a>
      </p>
    </footer>
  );
}
