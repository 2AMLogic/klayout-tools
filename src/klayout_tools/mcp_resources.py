"""The JSON-contract schemas ``klt mcp serve`` exposes as MCP resources.

Issue #5420. The derived tools of :mod:`klayout_tools.mcp_tools` expose a
verb's JSON request document as a *path* string, so an agent still needs the
request's own schema to write a valid document. The bridge therefore serves
every ``docs/schemas/*.schema.json`` file as an MCP resource, so a client
connected to ``klt mcp serve`` can fetch the contract without a checkout of
this repository.

Like the tool derivation, this module imports no MCP SDK: listing, reading and
the unknown-URI refusal are unit-testable without the optional ``[mcp]``
extra. The SDK-facing glue is in :mod:`klayout_tools.mcp_server`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

#: URI scheme for the schema resources: ``klt://schemas/<file name>``, e.g.
#: ``klt://schemas/socket.schema.json``. Documented in
#: ``docs/guides/mcp-server.md``; changing it is a breaking change.
SCHEMA_URI_PREFIX = "klt://schemas/"

#: MIME type every schema resource reports.
SCHEMA_MIME_TYPE = "application/json"

#: Filename suffix that makes a file in the schema directory a schema.
SCHEMA_SUFFIX = ".schema.json"

#: The schema directory bundled inside an installed package: `pyproject.toml`
#: force-includes `docs/schemas` to `klayout_tools/data/schemas`, so this
#: resolves next to the installed module rather than a fixed number of
#: `.parent` hops above it. It is the only location that survives
#: `pip install` / `uv tool install` -- a wheel unpacks into `site-packages`
#: with no sibling `docs/` directory (the same two-step lookup
#: `design_evidence_tiers.py` uses for the tier doc, issue #1050).
_PACKAGED_SCHEMA_DIR: Path = Path(__file__).resolve().parent / "data" / "schemas"

#: The canonical `docs/schemas/` in a source checkout (or an editable
#: install, whose `klayout_tools` package still lives at `src/klayout_tools`
#: in the repo): module -> `src/klayout_tools` -> `src` -> repo root. Used
#: only when the bundled copy above is absent, i.e. exactly when the schemas
#: have not been packaged alongside the module.
_SOURCE_SCHEMA_DIR: Path = (
    Path(__file__).resolve().parent.parent.parent / "docs" / "schemas"
)


def schema_dir() -> Path:
    """Resolve the directory the schema resources are served from: the copy
    bundled inside the installed package when present, else the source
    checkout's ``docs/schemas/``.

    Resolved per call (not frozen at import) so a missing bundled copy in one
    layout never poisons another.
    """
    if _PACKAGED_SCHEMA_DIR.is_dir():
        return _PACKAGED_SCHEMA_DIR
    return _SOURCE_SCHEMA_DIR


@dataclass(frozen=True)
class SchemaResource:
    """One ``*.schema.json`` file exposed as an MCP resource."""

    uri: str
    name: str
    title: str
    description: str
    path: Path
    size: int


class ResourceReadRefused(Exception):
    """A resource read was refused: no resource is served at that URI."""


def _schema_metadata(path: Path) -> tuple[str, str]:
    """``(title, description)`` taken from the schema's own keywords.

    Best-effort: a file that is not a JSON object, or is unreadable, yields
    empty strings rather than failing server start-up over one bad schema.
    """
    try:
        doc = json.loads(path.read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return "", ""
    if not isinstance(doc, dict):
        return "", ""
    title, description = doc.get("title"), doc.get("description")
    return (
        title if isinstance(title, str) else "",
        description if isinstance(description, str) else "",
    )


def derive_resources(directory: Path | None = None) -> list[SchemaResource]:
    """One :class:`SchemaResource` per ``*.schema.json`` in the schema
    directory (:func:`schema_dir` unless ``directory`` overrides it), ordered
    by file name so the listing is stable across calls and installs."""
    root = schema_dir() if directory is None else directory
    out: list[SchemaResource] = []
    for path in sorted(root.glob(f"*{SCHEMA_SUFFIX}")):
        if not path.is_file():
            continue
        title, description = _schema_metadata(path)
        out.append(
            SchemaResource(
                uri=SCHEMA_URI_PREFIX + path.name,
                name=path.name,
                title=title or path.name,
                description=description
                or f"{path.name} from the klayout-tools JSON contract.",
                path=path,
                size=path.stat().st_size,
            )
        )
    return out


def read_resource(uri: str, resources: list[SchemaResource] | None = None) -> str:
    """The document served at ``uri``, decoded UTF-8 and otherwise verbatim.

    The URI is resolved by **lookup in the enumerated resource set**, never
    by joining it onto a directory, so traversal is impossible by
    construction: ``klt://schemas/../../etc/passwd`` is simply not a key and
    raises :class:`ResourceReadRefused`, as does any name that is not one of
    the listed schemas.
    """
    listing = derive_resources() if resources is None else resources
    match = {r.uri: r for r in listing}.get(uri)
    if match is None:
        raise ResourceReadRefused(f"unknown resource URI {uri!r}")
    try:
        return match.path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ResourceReadRefused(f"cannot read resource {uri!r}: {exc}") from exc
