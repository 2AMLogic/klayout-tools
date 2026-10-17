"""``klt mcp serve``: stdio MCP server bridging the verb registry (issue #2830).

stdout is the MCP protocol channel here, so this verb has no ``--format``
flag and never prints to stdout; diagnostics go to stderr. Setup errors
(missing ``[mcp]`` extra, bad workspace root) use the shared error envelope
on stderr and exit ``1``.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from .output import emit_error


def run_serve(args: argparse.Namespace) -> int:
    from ..mcp_server import McpUnavailableError, serve
    from ..mcp_tools import BridgePolicy, registered_verbs

    root = Path(args.workspace_root).expanduser().resolve()
    if not root.is_dir():
        return emit_error(
            "mcp serve", f"workspace root {str(root)!r} is not a directory", "json"
        )
    known = set(registered_verbs())
    unknown = sorted(set(args.enable_verb) - known)
    if unknown:
        return emit_error(
            "mcp serve",
            f"unknown verb(s) for --enable-verb: {', '.join(unknown)}",
            "json",
            exit_code=2,
        )
    policy = BridgePolicy(
        workspace_root=root,
        enabled_verbs=frozenset(args.enable_verb),
        allow_fleet=args.allow_fleet,
        allow_outside_workspace=args.allow_outside_workspace,
        timeout_s=args.timeout,
    )
    try:
        serve(policy)
    except McpUnavailableError as exc:
        return emit_error("mcp serve", str(exc), "json")
    except KeyboardInterrupt:
        return 0
    return 0
