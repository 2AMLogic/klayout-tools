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

    async def _list_tools(ctx, params) -> types.ListToolsResult:
        return types.ListToolsResult(
            tools=[
                types.Tool(
                    name=t.name, description=t.description, input_schema=t.input_schema
                )
                for t in tools
            ]
        )

    async def _call_tool(ctx, params: types.CallToolRequestParams):
        name, arguments = params.name, params.arguments
        tool = by_name.get(name)
        if tool is None:
            payload = {
                "schema_version": 1,
                "error": {"command": name, "message": f"unknown tool {name!r}"},
            }
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=json.dumps(payload))],
                structured_content=payload,
                is_error=True,
            )
        outcome = await asyncio.to_thread(execute, tool, arguments or {}, policy)
        return types.CallToolResult(
            content=[
                types.TextContent(
                    type="text", text=json.dumps(outcome.payload, indent=2)
                )
            ],
            structured_content=outcome.payload,
            is_error=outcome.is_error,
            meta={"klt_exit_code": outcome.exit_code, "klt_argv": outcome.argv},
        )

    return Server(SERVER_NAME, on_list_tools=_list_tools, on_call_tool=_call_tool)


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
