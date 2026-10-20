"""Opt-in committable-evidence rewriting for ``klt place-and-route`` responses
(issue #3820).

The default response echoes absolute host paths (``def_path``, ``gds_path``,
``verilog_path``, ``layer_map.path``, per-stage and power-audit ``def_path``,
...) because they double as functional hand-offs (``klt extract --def-pins``,
``klt signoff``). A request carrying ``portable_paths: true`` swaps every such
string for a machine-independent form *at the point the response is returned*;
the flow itself keeps working on the real paths.

- inside the repo      -> ``{"path": "<repo-relative>", "scope": "repo"}``
- inside the PDK root  -> ``"$PDK_ROOT/<relative>"`` (the token
  :mod:`klayout_tools.synthesize` already uses)
- anywhere else        -> ``{"path": null, "scope": "external"}``

The walk is by value (any string that is an absolute path), not by a field
list, so a path-bearing field added later is covered without touching this
module.
"""

from __future__ import annotations

import os
from typing import Any

from .env_provenance import _absolute_paths_in, find_repo_root, repo_relative_path

PDK_ROOT_TOKEN = "$PDK_ROOT"


def _under(path: str, root: str) -> str | None:
    """``path`` relative to ``root`` (POSIX, ``"."`` for the root itself), or
    ``None`` when it is not inside it."""
    resolved = os.path.realpath(path)
    base = os.path.realpath(root)
    if resolved == base:
        return "."
    if not resolved.startswith(base.rstrip(os.sep) + os.sep):
        return None
    return os.path.relpath(resolved, base).replace(os.sep, "/")


def _portable_value(value: str, *, repo_root: str | None, pdk_root: str | None) -> Any:
    if pdk_root is not None:
        relative = _under(value, pdk_root)
        if relative is not None:
            return PDK_ROOT_TOKEN if relative == "." else f"{PDK_ROOT_TOKEN}/{relative}"
    return repo_relative_path(value, repo_root=repo_root)


def _portable_text(text: str, *, repo_root: str | None, pdk_root: str | None) -> str:
    """``text`` (a message, not a bare path) with each embedded absolute path
    replaced by its string form: ``$PDK_ROOT/...``, a repo-relative path, or
    ``<external>``."""
    for found in sorted(set(_absolute_paths_in(text, ())), key=len, reverse=True):
        value = _portable_value(found, repo_root=repo_root, pdk_root=pdk_root)
        if isinstance(value, dict):
            value = value["path"] if value["path"] is not None else "<external>"
        text = text.replace(found, value)
    return text


def portable_response(
    response: Any, *, request_dir: str, pdk_root: str | None = None
) -> Any:
    """``response`` with every absolute-path string replaced per the module
    docstring. Returns a new structure; the input is not mutated."""
    repo_root = find_repo_root(request_dir)

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            return {key: walk(item) for key, item in node.items()}
        if isinstance(node, list):
            return [walk(item) for item in node]
        if isinstance(node, str) and os.path.isabs(node):
            return _portable_value(node, repo_root=repo_root, pdk_root=pdk_root)
        if isinstance(node, str) and "/" in node:
            return _portable_text(node, repo_root=repo_root, pdk_root=pdk_root)
        return node

    return walk(response)
