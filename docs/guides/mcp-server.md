# The `klt mcp serve` MCP server

`klt mcp serve` exposes the `klt` verbs to any [Model Context Protocol](https://modelcontextprotocol.io)
client (Claude Desktop, Claude Code, Cursor, agent frameworks) over **stdio**.
It is a *generic bridge generated from the argparse registry*, not a
hand-written wrapper per verb (issue #2830, Phase 5 of `ROADMAP.md`).

## Install

The MCP SDK is an optional extra; the base install gains no dependency.

```
pip install 'klayout-tools[mcp]'      # or: uv sync --extra mcp
```

Without the extra, `klt mcp serve` exits `1` with a JSON error envelope on
stderr naming the extra to install.

## Client configuration

```json
{
  "mcpServers": {
    "klayout-tools": {
      "command": "klt",
      "args": ["mcp", "serve", "--workspace-root", "/path/to/your/block-repo"]
    }
  }
}
```

With `uv` from a checkout: `"command": "uv", "args": ["run", "--extra", "mcp",
"klt", "mcp", "serve", "--workspace-root", "."]`.

## How tools are derived

- One tool per leaf verb, named `klt_<verb>` (`klt_layers`, `klt_drc`); nested
  verbs add the subcommand (`klt_pdk_list`, `klt_wave_build`, `klt_kb_show`).
  Dashes become underscores. The `mcp` verb itself is not exposed.
- The `inputSchema` is built from the verb's argparse actions: positionals
  and `required=True` options are `required`; flags are `boolean`;
  `type=int/float` become `integer`/`number`; `choices` become `enum`;
  repeatable / multi-value options become `array`. Property names are the
  argparse `dest` (`--include-text` -> `include_text`).
- The description is the verb's help text plus a link to its `docs/cli/<verb>.md`.
- `--format` is owned by the bridge (always `json`); `--color` is not exposed.

## Execution and the contract

Each call runs `python -m klayout_tools.cli <verb> ... --format json` in a
subprocess with the workspace root as cwd. There is no in-process path, so
[`docs/json-contract.md`](../json-contract.md) stays the single contract:

| `klt` exit | MCP result |
|---|---|
| `0`, or any code `>= 3` (a report was emitted, e.g. `klt drc` exit `3`) | `isError: false`; `structuredContent` is the stdout JSON (also as text content). The raw exit code is in `_meta.klt_exit_code`. Read `status` for the verdict. |
| `1` / `2` | `isError: true`; `structuredContent` is the stderr error envelope `{"schema_version": 1, "error": {"command", "message"}}`. |

A call refused by a safety rule, a timeout, or a launch failure is also
returned as `isError: true` with the same envelope shape.

## Safety defaults

- **Fleet-dispatching verbs are hidden.** `yield-campaign` is not listed
  unless started with `--enable-verb yield-campaign` (or `--allow-fleet`).
- **Remote/batch backends are refused.** A call with `backend: "remote"` or
  `"batch"` (`klt sim`, `klt characterize`, ...) is rejected unless the server
  was started with `--allow-fleet`, since those provision EC2 hosts or submit
  to the batch fleet.
- **Workspace root.** Relative paths resolve against `--workspace-root`
  (default: the server's cwd), which is also each subprocess's cwd. Path-like
  arguments that resolve outside it (`../..`, absolute paths elsewhere) are
  rejected unless `--allow-outside-workspace` is given. The path check is
  heuristic (argument name/help text), not a sandbox: a verb's own request
  documents can still name files, so run the server in a directory and
  account you are comfortable exposing.
- `--timeout SECONDS` bounds each call (default 900).

## Resources: the JSON-contract schemas

Verbs that take a JSON request document expose the document *path* as a
string, so the server also serves every `docs/schemas/*.schema.json` file as
an MCP **resource** (issue #5420). A client can fetch the contract it needs to
write a valid request without a checkout of this repository.

| | |
|---|---|
| URI | `klt://schemas/<file name>`, e.g. `klt://schemas/socket.schema.json` |
| MIME type | `application/json` |
| `resources/list` | one entry per `*.schema.json`, ordered by file name; `title` and `description` are the schema's own keywords, `size` its byte length |
| `resources/read` | the schema document verbatim, as `text` |

A URI is resolved by lookup in that enumerated listing, never by joining it
onto a directory, so an unlisted or traversal URI
(`klt://schemas/../../etc/passwd`) is refused with a JSON-RPC
`InvalidParams` error rather than reading anything. Resource subscriptions and
resource templates are not served.

The schemas are shipped inside the wheel (`pyproject.toml` force-includes
`docs/schemas` to `klayout_tools/data/schemas`), so a `pip install
'klayout-tools[mcp]'` with no source checkout serves the same listing as a
checkout does.

## Limits of the generic schema

Verbs that take a JSON request document (`sim`, `lvs`, `wave`, ...) expose the
document *path* as a string. The request schemas that exist live under
`docs/schemas/` and are served as resources (above) --
`layout-plan-request`, `wave-build-request` and `wave-query-request`; the rest
of `docs/schemas/` describes responses, reports, manifests and descriptors, and
not every request-taking verb has a schema yet. Nothing links a tool to its
schema automatically. Argparse mutually-exclusive groups are not encoded in
the schema (the CLI rejects bad combinations with exit `2`). Richer per-verb
tools, MCP prompts, an HTTP transport, and the LLM reasoning module remain out
of scope.
