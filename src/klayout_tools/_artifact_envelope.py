"""Committable artifact envelope with a content digest (issue #4931)."""

from __future__ import annotations

import enum
import os
from typing import Any, Final

from ._provenance import content_hash
from .env_provenance import find_repo_root, repo_relative_path


class _Anchor(enum.Enum):
    CWD = "cwd"


#: ``repo_root`` default: the repo containing the process's cwd, the historical
#: anchor. A sentinel because ``None`` is already taken -- it means "no repo".
ANCHOR_CWD: Final = _Anchor.CWD

RepoRoot = str | os.PathLike[str] | None | _Anchor


def path_envelope(
    path: str | os.PathLike[str] | None, *, repo_root: RepoRoot = ANCHOR_CWD
) -> dict[str, Any]:
    """``path`` as the committable ``{path, scope}`` envelope (relative to
    ``repo_root``, never the absolute host path) -- the digest-free
    counterpart :func:`hashed_path_envelope` adds ``sha256`` to, and the
    shape the OpenROAD log recorder's fields use (issue #6764).

    ``repo_root`` is the repo the ``"repo"`` scope is relative to; ``None``
    means there is none (every present path ``external`` -- not
    :func:`repo_relative_path`'s own ``None`` default, which rediscovers a
    repo from the path). The default is
    the invoking cwd's repo, which varies per invocation: a verb that reads
    a request document passes the request's own repo root instead, so one
    run yields one committed report from any directory (issue #6762)."""
    root = find_repo_root() if repo_root is ANCHOR_CWD else repo_root
    if path is not None and root is None:
        return {"path": None, "scope": "external"}
    return repo_relative_path(path, repo_root=root)


def hashed_path_envelope(
    path: str | None, *, repo_root: RepoRoot = ANCHOR_CWD
) -> dict[str, Any]:
    """``path`` as the committable ``{path, scope}`` envelope (relative to
    ``repo_root``, never the absolute host path) plus ``sha256``: the
    ``sha256:``-prefixed digest of the file as it stands now, or ``None`` when
    it was not produced. Call after the producing process closed the file.

    ``repo_root`` is the repo the ``"repo"`` scope is relative to; ``None``
    means there is none (everything is ``external``). The default is the
    invoking cwd's repo, which varies per invocation: a verb that reads a
    request document passes the request's own repo root instead, so one run
    yields one committed report from any directory (issue #6762)."""
    envelope = path_envelope(path, repo_root=repo_root)
    envelope["sha256"] = content_hash(path)
    return envelope


def source_defines_envelope(
    source_defines: dict[str, Any], repo_root: RepoRoot = ANCHOR_CWD
) -> list[dict[str, Any]]:
    """A **path-keyed** mapping as the committable list form (issue #5784): an
    envelope cannot be a JSON object key, and a source outside the repo has no
    path to key on at all. Each resolved absolute key becomes its own
    :func:`hashed_path_envelope` (same ``repo_root``, #6762) under ``source``;
    ``[]`` when empty."""
    return [
        {"source": hashed_path_envelope(path, repo_root=repo_root), "defines": defines}
        for path, defines in source_defines.items()
    ]
