"""Field walker for the absolute-path lint in :mod:`klayout_tools.env_provenance`.

Split out so ``env_provenance.py`` stays under its per-file size ceiling.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from typing import Any


def _worker_environment_parent(parts: tuple[str | int, ...]) -> bool:
    """Match a remote environment from sim or one of PEX's two simulation sides."""
    if len(parts) >= 2 and parts[-2] == "fleet" and isinstance(parts[-1], int):
        parts = parts[:-2]
    match parts:
        case ("environment", "remote"):
            return True
        case (
            "testbenches",
            int(),
            "sides",
            "schematic" | "extracted",
            "environment",
            "remote",
        ):
            return True
        case _:
            return False


def walk_string_fields(value: Any, path: str) -> Iterable[tuple[str, str]]:
    """Every string in a JSON-decoded ``value`` as ``(dotted_field, text)``.

    Mapping *keys* are yielded too (a dict keyed by an absolute path leaks
    just as thoroughly as one valued by it), and list elements are addressed
    by index -- the same dotted-path convention ``_report_verify
    .diff_verdict_fields`` reports drift under, so a field named by one tool
    is findable with the other.
    """
    return _walk(value, path, None)


def _walk(
    value: Any, path: str, lint_parts: tuple[str | int, ...] | None
) -> Iterable[tuple[str, str]]:
    """Keep JSON keys and array indices distinct when scoping the lint exemption."""
    if isinstance(value, Mapping):
        for key, item in value.items():
            if (
                lint_parts is not None
                and key == "worker_environment"
                and _worker_environment_parent(lint_parts)
            ):
                continue
            child = f"{path}.{key}" if path else str(key)
            if isinstance(key, str):
                yield child, key
            yield from _walk(
                item, child, (*lint_parts, str(key)) if lint_parts is not None else None
            )
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            child = f"{path}.{index}" if path else str(index)
            yield from _walk(
                item, child, (*lint_parts, index) if lint_parts is not None else None
            )
        return
    if isinstance(value, str):
        yield (path or "<root>"), value


def walk_lintable_string_fields(payload: Any) -> Iterable[tuple[str, str]]:
    """:func:`walk_string_fields` minus the remote-worker observations."""
    return _walk(payload, "", ())


def common_repo_root(
    paths: Iterable[str | os.PathLike[str] | None],
    *,
    find_repo_root: Any,
) -> str | None:
    """One repo root for a run that records several input paths (#5631).

    Each non-``None`` path contributes ``find_repo_root`` of its own directory
    (``None`` results are dropped, so an input outside every repo never
    constrains the others). Returns the candidate that contains every other
    candidate -- the outermost when repos are nested -- so ``{path, scope}``
    fields of one report all share one anchor. When no candidate contains the
    rest (unrelated sibling repos) the first path's root wins and the others
    normalise to ``scope: "external"``. ``None`` when no path has a root.
    """
    roots: list[str] = []
    for path in paths:
        if path is None:
            continue
        root = find_repo_root(os.path.dirname(os.path.abspath(path)))
        if root is not None:
            roots.append(root)
    if not roots:
        return None
    for cand in roots:
        if all(r == cand or r.startswith(cand.rstrip(os.sep) + os.sep) for r in roots):
            return cand
    return roots[0]
