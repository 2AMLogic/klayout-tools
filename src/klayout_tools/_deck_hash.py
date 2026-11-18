"""Hash a named deck's rule sources, from the install, a build tree or a tag.

The literal DRC_RULE_SOURCES tuple in a primary module names sibling modules
that contribute to its curated rules. Read the declaration as data so build
and release tooling need neither KLayout nor imports from a historical tag.
"""

from __future__ import annotations

import ast
import hashlib
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=8)
def _siblings(primary: bytes) -> tuple[str, ...]:
    for node in ast.parse(primary).body:
        match node:
            case (
                ast.Assign(targets=[ast.Name(id="DRC_RULE_SOURCES")], value=value)
                | ast.AnnAssign(target=ast.Name(id="DRC_RULE_SOURCES"), value=value)
            ) if value is not None:
                names = ast.literal_eval(value)
            case _:
                continue
        if not isinstance(names, tuple) or not all(
            isinstance(name, str) and name.isidentifier() for name in names
        ):
            raise ValueError("DRC_RULE_SOURCES must be a tuple of sibling module names")
        return names
    return ()


def source_hash(filename: str, read: Callable[[str], bytes]) -> str:
    """Return the versioned source identity using only same-directory basenames."""
    primary = read(filename)
    raw = hashlib.sha256(primary).digest()
    # Older releases and singleton modules use the original raw file digest.
    siblings = (
        _siblings(primary)
        if filename.endswith(".py") and b"DRC_RULE_SOURCES" in primary
        else ()
    )
    if not siblings:
        return f"sha256:{raw.hex()}"
    parts = {filename: raw}
    for module in siblings:
        sibling = f"{module}.py"
        parts[sibling] = hashlib.sha256(read(sibling)).digest()
    digest = hashlib.sha256()
    for basename, file_hash in sorted(parts.items()):
        digest.update(basename.encode() + b"\0" + file_hash)
    return f"sha256:{digest.hexdigest()}"


def source_file_hash(path: str | None) -> str | None:
    if not path:
        return None
    primary = Path(path)
    try:
        return source_hash(
            primary.name, lambda name: (primary.parent / name).read_bytes()
        )
    except OSError:
        return None
