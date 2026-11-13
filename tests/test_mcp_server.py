"""Tests for the generic ``klt mcp serve`` bridge (issue #2830).

The derivation/execution half (``klayout_tools.mcp_tools``) and the schema
resources (``klayout_tools.mcp_resources``) need no MCP SDK, so those tests
always run. The stdio round-trips need the optional ``[mcp]`` extra and are
skipped without it, keeping the base-install suite green.
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


def test_backend_forced_local_without_allow_fleet(tools, policy):
    backend_tools = [t for t in tools if "backend" in t.input_schema["properties"]]
    assert {t.name for t in backend_tools} >= {"klt_sim", "klt_netlist"}
    fleet = BridgePolicy(workspace_root=REPO, allow_fleet=True)
    for tool in backend_tools:
        args = {
            d: "x.json" for d in tool.input_schema.get("required", []) if d != "backend"
        }
        argv = build_argv(tool, args, policy)
        assert argv[argv.index("--backend") + 1] == "local", tool.name
        assert "--backend" not in build_argv(tool, args, fleet), tool.name
        local = build_argv(tool, {**args, "backend": "local"}, policy)
        assert local.count("--backend") == 1


def test_option_injection_refused(tools, policy):
    from klayout_tools.mcp_tools import ToolCallRefused

    sim = _tool(tools, "klt_sim")
    with pytest.raises(ToolCallRefused):
        build_argv(sim, {"request": ["req.json", "--backend", "batch"]}, policy)
    with pytest.raises(ToolCallRefused, match="option"):
        build_argv(sim, {"request": "--backend"}, policy)
    with pytest.raises(ToolCallRefused):
        build_argv(sim, {"request": "req.json", "backend": ["local", "x"]}, policy)
    array_tool = next(
        t
        for t in tools
        if any(
            p.get("type") == "array" and d in t.path_dests
            for d, p in t.input_schema["properties"].items()
        )
    )
    dest = next(
        d
        for d, p in array_tool.input_schema["properties"].items()
        if p.get("type") == "array" and d in array_tool.path_dests
    )
    # Satisfy the tool's other required arguments so the refusal under test is
    # the option check, not "missing required argument(s)".
    props = array_tool.input_schema["properties"]
    others = {
        d: ["x"] if props[d].get("type") == "array" else "x"
        for d in array_tool.input_schema.get("required", [])
        if d != dest
    }
    with pytest.raises(ToolCallRefused, match="option"):
        build_argv(array_tool, {**others, dest: ["a", "-x"]}, policy)


def test_injection_refused_before_subprocess(tools, policy, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("subprocess launched")

    monkeypatch.setattr(subprocess, "run", boom)
    outcome = execute(
        _tool(tools, "klt_sim"),
        {"request": ["req.json", "--backend", "batch"]},
        policy,
    )
    assert outcome.is_error and outcome.exit_code == 2
    assert outcome.payload["schema_version"] == 1


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
    assert not good.is_error
    assert good.structured_content == json.loads(cli.stdout)
    assert bad.is_error
    assert bad.structured_content["error"]["command"] == "layers"


# --------------------------------------------------------------------------- #
# Schema resources (issue #5420): `docs/schemas/*.schema.json` served over MCP
# so a client can fetch the JSON contract without a checkout of this repo.
# --------------------------------------------------------------------------- #

SCHEMA_DIR = REPO / "docs" / "schemas"


@pytest.fixture(scope="module")
def resources():
    from klayout_tools.mcp_resources import derive_resources

    return derive_resources()


def test_resources_list_one_per_schema_file(resources):
    from klayout_tools.mcp_resources import SCHEMA_MIME_TYPE, SCHEMA_URI_PREFIX

    on_disk = sorted(p.name for p in SCHEMA_DIR.glob("*.schema.json"))
    assert on_disk, "expected docs/schemas to hold schema files"
    assert [r.name for r in resources] == on_disk, (
        "one resource per schema file, ordered by file name"
    )
    assert [r.uri for r in resources] == [SCHEMA_URI_PREFIX + n for n in on_disk]
    assert len({r.uri for r in resources}) == len(resources), "URIs must be unique"
    assert SCHEMA_MIME_TYPE == "application/json"
    for r in resources:
        # Listing metadata comes from the schema's own keywords.
        assert r.title and r.description
        assert r.size == r.path.stat().st_size
        json.dumps({"uri": r.uri, "name": r.name, "description": r.description})


def test_reading_a_resource_returns_the_file_bytes_unchanged(resources):
    from klayout_tools.mcp_resources import read_resource

    for r in resources:
        assert read_resource(r.uri, resources).encode("utf-8") == r.path.read_bytes()
    # The default argument derives the listing itself.
    socket = "klt://schemas/socket.schema.json"
    assert json.loads(read_resource(socket))["title"]


@pytest.mark.parametrize(
    "uri",
    [
        "klt://schemas/../../etc/passwd",
        "klt://schemas//etc/passwd",
        "klt://schemas/socket.schema.json/../socket.schema.json",
        "klt://schemas/nope.schema.json",
        "klt://schemas/README.md",
        "klt://schemas/",
        "docs/schemas/socket.schema.json",
        "file:///etc/passwd",
        "",
    ],
)
def test_unknown_resource_uri_is_refused(resources, uri):
    from klayout_tools.mcp_resources import ResourceReadRefused, read_resource

    with pytest.raises(ResourceReadRefused, match="unknown resource URI"):
        read_resource(uri, resources)


def test_schema_dir_prefers_the_packaged_copy(monkeypatch, tmp_path):
    """A wheel install has no sibling `docs/` directory, so the schemas must
    resolve against the installed package first (the two-step lookup
    `design_evidence_tiers.py` uses for the tier doc, issue #1050)."""
    from klayout_tools import mcp_resources

    packaged = tmp_path / "site-packages" / "klayout_tools" / "data" / "schemas"
    packaged.mkdir(parents=True)
    (packaged / "only.schema.json").write_text('{"title": "t"}', encoding="utf-8")
    monkeypatch.setattr(mcp_resources, "_PACKAGED_SCHEMA_DIR", packaged)
    monkeypatch.setattr(
        mcp_resources,
        "_SOURCE_SCHEMA_DIR",
        tmp_path / "site-packages" / "docs" / "schemas",
    )

    assert mcp_resources.schema_dir() == packaged
    assert [r.name for r in mcp_resources.derive_resources()] == ["only.schema.json"]


def test_schema_dir_falls_back_to_the_source_checkout(monkeypatch, tmp_path):
    from klayout_tools import mcp_resources

    monkeypatch.setattr(mcp_resources, "_PACKAGED_SCHEMA_DIR", tmp_path / "not-bundled")
    monkeypatch.setattr(mcp_resources, "_SOURCE_SCHEMA_DIR", SCHEMA_DIR)

    assert mcp_resources.schema_dir() == SCHEMA_DIR


def test_resource_metadata_survives_an_unparseable_schema(tmp_path):
    """One bad file must not fail the whole listing: start-up derivation is
    best-effort about a schema's own `title`/`description`."""
    from klayout_tools.mcp_resources import derive_resources, read_resource

    (tmp_path / "broken.schema.json").write_text("{not json", encoding="utf-8")
    (tmp_path / "list.schema.json").write_text("[1, 2]", encoding="utf-8")
    (tmp_path / "notes.md").write_text("ignored", encoding="utf-8")

    listed = derive_resources(tmp_path)

    assert [r.name for r in listed] == ["broken.schema.json", "list.schema.json"]
    for r in listed:
        assert r.title == r.name and r.name in r.description
    assert read_resource("klt://schemas/broken.schema.json", listed) == "{not json"


def test_wheel_force_includes_the_schema_directory():
    """Mechanical drift guard: `pyproject.toml` must force-include
    `docs/schemas` at exactly the package-relative path `_PACKAGED_SCHEMA_DIR`
    resolves to, so packaging config and resolution cannot diverge."""
    # `tomllib` is stdlib only from 3.11 (PEP 680); this project supports 3.10.
    tomllib = pytest.importorskip("tomllib")
    from klayout_tools import mcp_resources

    cfg = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    force = cfg["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]

    package_dir = Path(mcp_resources.__file__).resolve().parent
    expected = str(mcp_resources._PACKAGED_SCHEMA_DIR.relative_to(package_dir.parent))
    assert force["docs/schemas"] == expected


def test_built_wheel_carries_every_schema(tmp_path):
    """The schemas must be in the artifact, not only in a source checkout
    (acceptance criterion 3): build this repo's wheel and read its namelist."""
    import shutil
    import zipfile

    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("needs the `uv` CLI to build a wheel")
    from klayout_tools import mcp_resources

    out = tmp_path / "dist"
    build = subprocess.run(
        [uv, "build", "--wheel", "--out-dir", str(out)],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert build.returncode == 0, f"uv build failed:\n{build.stdout}\n{build.stderr}"
    (wheel,) = out.glob("*.whl")

    package_dir = Path(mcp_resources.__file__).resolve().parent
    prefix = str(mcp_resources._PACKAGED_SCHEMA_DIR.relative_to(package_dir.parent))
    with zipfile.ZipFile(wheel) as archive:
        shipped = {
            n.rsplit("/", 1)[1]
            for n in archive.namelist()
            if n.startswith(f"{prefix}/") and n.endswith(".schema.json")
        }
    assert shipped == {p.name for p in SCHEMA_DIR.glob("*.schema.json")}


def test_stdio_round_trip_serves_the_schema_resources():
    pytest.importorskip("mcp")
    import anyio
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from mcp.shared.exceptions import MCPError

    from klayout_tools.mcp_resources import SCHEMA_MIME_TYPE

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "klayout_tools.cli", "mcp", "serve", "--workspace-root", str(REPO)],
    )
    uri = "klt://schemas/socket.schema.json"

    async def scenario():
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                init = await session.initialize()
                # Tools are untouched by the resources addition.
                assert init.capabilities.tools is not None
                assert init.capabilities.resources is not None, (
                    "the server must advertise the resources capability"
                )
                listed = (await session.list_resources()).resources
                contents = (await session.read_resource(uri)).contents
                with pytest.raises(MCPError) as unknown:
                    await session.read_resource("klt://schemas/../../etc/passwd")
                return listed, contents, unknown.value

    listed, contents, unknown = anyio.run(scenario)

    assert {r.uri for r in listed} == {
        "klt://schemas/" + p.name for p in SCHEMA_DIR.glob("*.schema.json")
    }
    assert all(r.mime_type == SCHEMA_MIME_TYPE for r in listed)
    (content,) = contents
    assert content.uri == uri and content.mime_type == SCHEMA_MIME_TYPE
    assert (
        content.text.encode("utf-8") == (SCHEMA_DIR / "socket.schema.json").read_bytes()
    )
    assert "unknown resource URI" in str(unknown)
