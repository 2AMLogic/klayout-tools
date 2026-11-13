"""Tests for the schema resources ``klt mcp serve`` exposes (issue #5420).

`docs/schemas/*.schema.json` is served over MCP so a client can fetch the JSON
contract without a checkout of this repository. The derivation half
(``klayout_tools.mcp_resources``) needs no MCP SDK, so those tests always run;
the handler and stdio tests need the optional ``[mcp]`` extra and are skipped
without it, keeping the base-install suite green.

Split out of ``tests/test_mcp_server.py``, which mirrors the source split
(``mcp_server.py`` is the SDK-facing glue, ``mcp_resources.py`` the
derivation): together they exceeded the 500-line per-file ratchet ceiling.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from klayout_tools.mcp_tools import BridgePolicy

REPO = Path(__file__).resolve().parent.parent
SCHEMA_DIR = REPO / "docs" / "schemas"


@pytest.fixture
def policy():
    return BridgePolicy(workspace_root=REPO)


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


# --------------------------------------------------------------------------- #
# The same three resource behaviours, in-process (issue #5420).
#
# `test_stdio_round_trip_serves_the_schema_resources` above proves them over a
# real wire, but it drives `klt mcp serve` in a *subprocess*, which the
# coverage run does not measure -- so every `build_server` handler body reads
# as untested and the coverage ratchet fails on `mcp_server.py`. These two
# tests call `build_server` in the test process and invoke the handlers it
# registered with the SDK, so the same paths are exercised where they can be
# measured. Both layers are kept: the wire test is the one that proves the
# protocol, these are the ones that prove (and measure) the handler bodies.
# --------------------------------------------------------------------------- #


@pytest.fixture
def server(policy):
    pytest.importorskip("mcp")
    from klayout_tools.mcp_server import build_server

    return build_server(policy)


def test_build_server_advertises_the_resources_capability(server):
    import mcp.types as types

    # Registering the two handlers is what makes the SDK advertise `resources`
    # at `initialize` (it derives capabilities from the handler table), so
    # assert the derived capability and the table it comes from.
    capabilities = server.get_capabilities()
    assert capabilities.resources is not None, (
        "the server must advertise the resources capability"
    )
    assert capabilities.tools is not None, "tools must stay advertised"

    list_entry = server.get_request_handler("resources/list")
    read_entry = server.get_request_handler("resources/read")
    assert list_entry is not None and read_entry is not None
    assert read_entry.params_type is types.ReadResourceRequestParams


def test_resource_handlers_list_read_and_refuse_in_process(server, resources):
    import anyio
    import mcp.types as types
    from mcp.shared.exceptions import MCPError

    from klayout_tools.mcp_resources import SCHEMA_MIME_TYPE

    # The handlers take `(ctx, params)` and neither reads `ctx`, so the request
    # context the SDK runner would build is not needed to exercise them.
    list_handler = server.get_request_handler("resources/list").handler
    read_handler = server.get_request_handler("resources/read").handler
    uri = "klt://schemas/socket.schema.json"

    async def scenario():
        listed = (await list_handler(None, None)).resources
        read = await read_handler(None, types.ReadResourceRequestParams(uri=uri))
        with pytest.raises(MCPError) as refused:
            await read_handler(
                None,
                types.ReadResourceRequestParams(uri="klt://schemas/../../etc/passwd"),
            )
        return listed, read, refused.value

    listed, read, refused = anyio.run(scenario)

    # Listing: one resource per schema file, carrying the derived metadata.
    assert [r.uri for r in listed] == [r.uri for r in resources]
    assert [r.name for r in listed] == [r.name for r in resources]
    assert [r.size for r in listed] == [r.size for r in resources]
    assert all(r.mime_type == SCHEMA_MIME_TYPE for r in listed)

    # Successful read: the file's bytes, unchanged, with the JSON MIME type.
    (content,) = read.contents
    assert content.uri == uri and content.mime_type == SCHEMA_MIME_TYPE
    assert (
        content.text.encode("utf-8") == (SCHEMA_DIR / "socket.schema.json").read_bytes()
    )

    # Refused read: INVALID_PARAMS, with the reason kept on the wire rather
    # than sanitised by the SDK runner to a generic internal error.
    assert refused.error.code == types.INVALID_PARAMS
    assert "unknown resource URI" in refused.error.message
