# `klt mcp`

```
klt mcp serve [--workspace-root DIR] [--enable-verb VERB] [--allow-fleet]
              [--allow-outside-workspace] [--timeout SECONDS]
```

Runs a stdio Model Context Protocol server exposing one tool per `klt` verb.
stdout is the protocol channel, so this verb has no `--format`; setup errors
(missing `[mcp]` extra, bad workspace root) use the shared JSON error envelope
on stderr with exit `1` (`2` for an unknown `--enable-verb`).

Needs `pip install 'klayout-tools[mcp]'`. Full behaviour, safety defaults, and
a client config snippet: [`docs/guides/mcp-server.md`](../guides/mcp-server.md).

Without `--allow-fleet` the bridge appends `--backend local` to every verb that
has a `--backend` option (`sim`, `netlist`, `pex`, `characterize`), so a
`"backend":"batch"` in a request file or `$KLT_SIM_BACKEND` cannot reach the
fleet. Argument shapes are checked against each tool's schema: a list in a
scalar slot, or a `-`-prefixed string value, is refused with the standard error
envelope before any subprocess starts.
