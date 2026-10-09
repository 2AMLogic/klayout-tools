"""Tests for the generic ``klt mcp serve`` bridge (issue #2830).

The derivation/execution half (``klayout_tools.mcp_tools``) needs no MCP SDK,
so those tests always run. The stdio round-trip needs the optional ``[mcp]``
extra and is skipped without it, keeping the base-install suite green.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from klayout_tools.mcp_tools import (
    HIDDEN_BY_DEFAULT,
    BridgePolicy,
    build_argv,
    derive_tools,
    execute,
    registered_verbs,
    visible_tools,
)

REPO = Path(__file__).resolve().parent.parent
GDS = "blocks/sky130_fd_sc_hd__inv_1/output/sky130_fd_sc_hd__inv_1.gds"


@pytest.fixture(scope="module")
def tools():
    return derive_tools()


@pytest.fixture
def policy():
    return BridgePolicy(workspace_root=REPO)


def _tool(tools, name):
    return next(t for t in tools if t.name == name)


def test_every_registered_verb_yields_a_valid_tool(tools):
    names = [t.name for t in tools]
    assert len(names) == len(set(names)), "tool names must be unique"
    for verb in registered_verbs():
        assert any(t.top_verb == verb for t in tools), f"no tool for verb {verb}"
    for t in tools:
        assert t.name.startswith("klt_") and t.name.replace("_", "").isalnum()
        assert t.description.startswith("klt ")
        schema = t.input_schema
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False
        for dest in schema.get("required", []):
            assert dest in schema["properties"]
        for prop in schema["properties"].values():
            assert prop["type"] in {"string", "integer", "number", "boolean", "array"}
        # Bridge-owned flags never leak into the schema.
        assert "format" not in schema["properties"]
        # json round-trips (an MCP client serialises it).
        json.dumps(schema)


def test_bridge_does_not_expose_itself(tools):
    assert not any(t.top_verb == "mcp" for t in tools)


def test_fleet_verbs_hidden_unless_opted_in(tools, policy):
    shown = {t.top_verb for t in visible_tools(tools, policy)}
    assert not (shown & HIDDEN_BY_DEFAULT)
    opted = BridgePolicy(
        workspace_root=REPO, enabled_verbs=frozenset(HIDDEN_BY_DEFAULT)
    )
    assert HIDDEN_BY_DEFAULT <= {t.top_verb for t in visible_tools(tools, opted)}
    fleet = BridgePolicy(workspace_root=REPO, allow_fleet=True)
    assert len(visible_tools(tools, fleet)) == len(tools)


def test_build_argv_translates_arguments(tools, policy):
    argv = build_argv(
        _tool(tools, "klt_layers"),
        {"file": GDS, "top": "x", "flattened": True, "include_text": False},
        policy,
    )
    assert argv == ["layers", GDS, "--top", "x", "--flattened", "--format", "json"]


def test_build_argv_rejects_bad_calls(tools, policy):
    layers = _tool(tools, "klt_layers")
    from klayout_tools.mcp_tools import ToolCallRefused

    with pytest.raises(ToolCallRefused, match="missing required"):
        build_argv(layers, {}, policy)
    with pytest.raises(ToolCallRefused, match="unknown argument"):
        build_argv(layers, {"file": GDS, "bogus": 1}, policy)
    with pytest.raises(ToolCallRefused, match="outside the workspace"):
        build_argv(layers, {"file": "../../etc/passwd"}, policy)
    with pytest.raises(ToolCallRefused, match="outside the workspace"):
        build_argv(layers, {"file": "/etc/passwd"}, policy)
    loose = BridgePolicy(workspace_root=REPO, allow_outside_workspace=True)
    build_argv(layers, {"file": "/etc/passwd"}, loose)


def test_fleet_backend_refused_by_default(tools, policy):
    from klayout_tools.mcp_tools import ToolCallRefused

    sim = _tool(tools, "klt_sim")
    assert "backend" in sim.input_schema["properties"]
    request = {"request": "req.json", "backend": "batch"}
    with pytest.raises(ToolCallRefused, match="--allow-fleet"):
        build_argv(sim, request, policy)
    build_argv(sim, request, BridgePolicy(workspace_root=REPO, allow_fleet=True))


def test_execute_matches_cli_json(tools, policy):
    outcome = execute(_tool(tools, "klt_layers"), {"file": GDS}, policy)
    assert not outcome.is_error
    cli = subprocess.run(
        [sys.executable, "-m", "klayout_tools.cli", "layers", GDS, "--format", "json"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    )
    assert outcome.payload == json.loads(cli.stdout)


def test_execute_failure_returns_error_envelope(tools, policy):
    outcome = execute(_tool(tools, "klt_layers"), {"file": "no-such.gds"}, policy)
    assert outcome.is_error
    assert outcome.exit_code == 1
    assert outcome.payload["schema_version"] == 1
    assert {"command", "message"} <= set(outcome.payload["error"])


def test_workspace_root_is_cwd(tools, tmp_path):
    outcome = execute(
        _tool(tools, "klt_layers"),
        {"file": "missing.gds"},
        BridgePolicy(workspace_root=tmp_path),
    )
    assert outcome.is_error and "missing.gds" in outcome.payload["error"]["message"]


def test_mcp_serve_without_sdk_gives_install_hint(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake(name, *a, **kw):
        if name == "mcp" or name.startswith("mcp."):
            raise ImportError("blocked")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", fake)
    from klayout_tools.mcp_server import McpUnavailableError, build_server

    with pytest.raises(McpUnavailableError, match=r"klayout-tools\[mcp\]"):
        build_server(BridgePolicy(workspace_root=REPO))


def test_stdio_round_trip():
    pytest.importorskip("mcp")
    import anyio
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "klayout_tools.cli", "mcp", "serve", "--workspace-root", str(REPO)],
    )

    async def scenario():
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                listed = (await session.list_tools()).tools
                names = {t.name for t in listed}
                assert "klt_layers" in names and "klt_stats" in names
                assert "klt_yield_campaign" not in names
                good = await session.call_tool("klt_layers", {"file": GDS})
                bad = await session.call_tool("klt_layers", {"file": "nope.gds"})
                return good, bad

    good, bad = anyio.run(scenario)
    cli = subprocess.run(
        [sys.executable, "-m", "klayout_tools.cli", "layers", GDS, "--format", "json"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    )
    assert not good.isError
    assert good.structuredContent == json.loads(cli.stdout)
    assert bad.isError
    assert bad.structuredContent["error"]["command"] == "layers"
