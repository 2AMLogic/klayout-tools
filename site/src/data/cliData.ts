/**
 * Typed view of `cli.generated.json` — the release, verb list, MCP tool count
 * and deck names the landing page shows (issue #4597).
 *
 * The JSON is generated from the `klt` CLI registry by
 * `scripts/gen_site_cli_data.py` and is NOT committed (gitignored since issue
 * #5005): `scripts/deploy-site.sh` and CI's `site/` job generate it before
 * `tsc -b`, so a fresh checkout must run
 * `uv run python scripts/gen_site_cli_data.py` once before type-checking or
 * testing the site. Never edit it by hand — regenerate it.
 */
import raw from "./cli.generated.json";

export interface CliVerb {
  name: string;
  summary: string;
  /** GitHub URL of the verb's `docs/cli/*.md` page, or null if it has none. */
  doc: string | null;
}

export interface CliGroup {
  id: string;
  title: string;
  verbs: CliVerb[];
}

export interface CliData {
  release: { version: string; date: string };
  verb_count: number;
  mcp_tool_count: number;
  decks: string[];
  groups: CliGroup[];
}

export const cliData: CliData = raw;
