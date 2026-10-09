"""stdio MCP server for ``klt mcp serve`` (issue #2830).

SDK-facing half of the generic bridge; the tool derivation and the subprocess
execution live in :mod:`klayout_tools.mcp_tools` (no SDK import there). This
module imports the optional ``mcp`` package lazily so the base install never
needs it -- :func:`serve` raises :class:`McpUnavailableError` with the install
hint when the ``[mcp]`` extra is missing.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from .mcp_tools import (
    BridgePolicy,
    Tool,
    derive_tools,
    execute,
    visible_tools,
)

SERVER_NAME = "klayout-tools"


class McpUnavailableError(RuntimeError):
    """The optional ``mcp`` SDK is not installed."""


def build_server(policy: BridgePolicy):
    """Build the low-level MCP ``Server`` exposing the derived tools."""
    try:
        import mcp.types as types
        from mcp.server.lowlevel import Server
    except ImportError as exc:  # pragma: no cover - exercised without the extra
        raise McpUnavailableError(
            "the MCP SDK is not installed; install it with "
            "`pip install 'klayout-tools[mcp]'` (or `uv sync --extra mcp`)"
        ) from exc

    tools = visible_tools(derive_tools(), policy)
    by_name: dict[str, Tool] = {t.name: t for t in tools}
    server = Server(SERVER_NAME)

    @server.list_tools()
    async def _list_tools() -> list[types.Tool]:
        return [
            types.Tool(
                name=t.name, description=t.description, inputSchema=t.input_schema
            )
            for t in tools
        ]

    @server.call_tool(validate_input=False)
    async def _call_tool(name: str, arguments: dict[str, Any] | None):
        tool = by_name.get(name)
        if tool is None:
            payload = {
                "schema_version": 1,
                "error": {"command": name, "message": f"unknown tool {name!r}"},
            }
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=json.dumps(payload))],
                structuredContent=payload,
                isError=True,
            )
        outcome = await asyncio.to_thread(execute, tool, arguments or {}, policy)
        return types.CallToolResult(
            content=[
                types.TextContent(
                    type="text", text=json.dumps(outcome.payload, indent=2)
                )
            ],
            structuredContent=outcome.payload,
            isError=outcome.is_error,
            _meta={"klt_exit_code": outcome.exit_code, "klt_argv": outcome.argv},
        )

    return server


async def _serve_async(policy: BridgePolicy) -> None:
    from mcp.server.stdio import stdio_server

    server = build_server(policy)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream, write_stream, server.create_initialization_options()
        )


def serve(policy: BridgePolicy) -> None:
    """Serve MCP over stdio until the client closes the stream."""
    build_server(policy)  # fail fast with the install hint before binding stdio
    asyncio.run(_serve_async(policy))
