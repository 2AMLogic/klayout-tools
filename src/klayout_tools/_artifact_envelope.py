"""Committable artifact envelope with a content digest (issue #4931)."""

from __future__ import annotations

from typing import Any

from ._provenance import content_hash
from .env_provenance import find_repo_root, repo_relative_path


def hashed_path_envelope(path: str | None) -> dict[str, Any]:
    """``path`` as the committable ``{path, scope}`` envelope (relative to the
    invoking repo, never the absolute host path) plus ``sha256``: the
    ``sha256:``-prefixed digest of the file as it stands now, or ``None`` when
    it was not produced. Call after the producing process closed the file."""
    root = find_repo_root()
    if path is not None and root is None:
        envelope: dict[str, Any] = {"path": None, "scope": "external"}
    else:
        envelope = repo_relative_path(path, repo_root=root)
    envelope["sha256"] = content_hash(path)
    return envelope
