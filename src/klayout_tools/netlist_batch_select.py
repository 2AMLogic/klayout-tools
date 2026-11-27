"""``--symbol-path`` validation for ``netlist_batch``, split out for the size
ratchet; the include / exclude glob filter it once held is
:mod:`klayout_tools._batch_tree`'s."""

from __future__ import annotations

import os
from collections.abc import Sequence

from .netlist import NetlistError


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
