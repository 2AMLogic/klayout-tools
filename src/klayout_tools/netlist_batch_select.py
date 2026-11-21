"""File-selection helpers for ``netlist_batch`` (split out for the size ratchet)."""

from __future__ import annotations

import fnmatch
import os
from collections.abc import Sequence

from .netlist import NetlistError

_GLOB_CHARS = frozenset("*?[")


def _matches_glob(rel: str, pattern: str) -> bool:
    """``rel`` (a root-relative POSIX path) against one ``--batch-include`` /
    ``--batch-exclude`` pattern. ``fnmatch`` semantics, so ``*`` also crosses
    ``/``; a pattern with no glob character names a file or a directory and
    matches that file or everything under that directory."""
    while pattern.startswith("./"):
        pattern = pattern[2:]
    if not any(char in _GLOB_CHARS for char in pattern):
        bare = pattern.rstrip("/")
        return rel == bare or rel.startswith(bare + "/")
    return fnmatch.fnmatchcase(rel, pattern)


def _select_files(
    files: list[str], include: Sequence[str], exclude: Sequence[str]
) -> list[str]:
    """``files`` kept by ``include`` (every file when empty) and not dropped
    by ``exclude``."""
    return [
        rel
        for rel in files
        if (not include or any(_matches_glob(rel, pat) for pat in include))
        and not any(_matches_glob(rel, pat) for pat in exclude)
    ]


def _relative_symbol_dirs(
    symbol_path: Sequence[str], root: str, selected: set[str]
) -> list[str]:
    """Validate each ``--symbol-path`` DIR and return it root-relative.

    Each must be an existing directory under the root with at least one
    selected tracked file (issue #4952); validated before any archive or S3
    write. Stored root-relative ('.' for the root itself).
    """
    symbol_rel: list[str] = []
    for entry in symbol_path:
        if not os.path.isdir(entry):
            raise NetlistError(f"--symbol-path is not a directory: {entry}")
        rel_dir = os.path.relpath(os.path.realpath(entry), root)
        if (
            rel_dir == os.pardir
            or rel_dir.startswith(os.pardir + os.sep)
            or os.path.isabs(rel_dir)
        ):
            raise NetlistError(
                f"--symbol-path {entry} is outside the batch root {root}; only "
                "files under the root are uploaded -- pass a wider --batch-root"
            )
        rel_dir = rel_dir.replace(os.sep, "/")
        prefix = "" if rel_dir == "." else rel_dir + "/"
        if not any(item.startswith(prefix) for item in selected):
            raise NetlistError(
                f"--symbol-path {entry} contains no git-tracked files in the "
                "selected upload set (check --batch-include / --batch-exclude, "
                "or `git add` the symbols); the runner would not have them"
            )
        symbol_rel.append(rel_dir)
    return symbol_rel
