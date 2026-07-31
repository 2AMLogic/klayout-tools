/**
 * Build-time provenance info for the site footer (issue #66).
 *
 * `getBuildSha` runs in Node during `astro build`/`astro dev` frontmatter —
 * never in the browser. Astro sites can be built from a context with no
 * `.git` directory (a tarball export, a shallow CI checkout, a Docker build
 * context that excludes VCS metadata), so the lookup must never throw and
 * must never fail the build. Fallback chain:
 *
 *   1. `git rev-parse --short HEAD` — local builds + deploys from a full
 *      checkout.
 *   2. `GITHUB_SHA` env var (40-char on GitHub Actions), sliced to 7 chars —
 *      covers CI contexts where git history is unavailable but the platform
 *      still exposes the commit via env.
 *   3. `"unknown"` — terminal fallback; the footer still renders.
 */

import { execSync as defaultExecSync } from "node:child_process";

export interface BuildSha {
  /** Short commit hash, or `"unknown"` when neither git nor env resolved one. */
  sha: string;
  /** `true` when `sha` came from `git rev-parse` (safe to link as a commit). */
  fromGit: boolean;
}

type ExecSyncFn = typeof defaultExecSync;

/**
 * Resolve the build-time short commit SHA.
 *
 * `env` and `execSync` are injectable for testing the fallback chain without
 * needing to actually leave a git checkout; production call sites use the
 * defaults (`process.env`, `node:child_process`'s `execSync`).
 */
export function getBuildSha(
  env: NodeJS.ProcessEnv = process.env,
  execSync: ExecSyncFn = defaultExecSync,
): BuildSha {
  try {
    const sha = execSync("git rev-parse --short HEAD", {
      encoding: "utf8",
      stdio: ["ignore", "pipe", "ignore"],
    }).trim();
    if (sha) return { sha, fromGit: true };
  } catch {
    // Not a git checkout (or git isn't installed) — fall through to the
    // env/unknown fallback below rather than letting the build fail.
  }

  const envSha = env.GITHUB_SHA;
  if (envSha) return { sha: envSha.slice(0, 7), fromGit: false };

  return { sha: "unknown", fromGit: false };
}

/** Build the GitHub commit URL for a resolved `BuildSha`, or the bare repo URL as a fallback. */
export function commitUrl(repoUrl: string, build: BuildSha): string {
  return build.sha !== "unknown" ? `${repoUrl}/commit/${build.sha}` : repoUrl;
}
