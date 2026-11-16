/**
 * Typed view of `cli.generated.json` — the release, verb list, MCP tool count
 * and deck names the landing page shows (issue #4597).
 *
 * The JSON is generated from the `klt` CLI registry by
 * `scripts/gen_site_cli_data.py` and committed; `tests/test_site_cli_data.py`
 * fails when it drifts from the code. Never edit it by hand — regenerate it.
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
