"""Normalize host source paths out of a Yosys-written gate-level netlist
(issue #4872).

Yosys names some internal nets after the path it was given for the source
file, e.g. ``\\f$func$/abs/dir/f.v:3$2.x`` (seen with sv2v output that has
functions with casts). When ``klt synthesize`` hands Yosys an absolute path,
the written netlist embeds the directory the request ran in, so identical
requests from different directories differ byte for byte and leak host paths.

:func:`normalize_source_paths` is a pure text transform: it drops each
source's absolute directory prefix, leaving ``f.v:3`` in the name.
"""

from __future__ import annotations

import os
from collections.abc import Iterable


def normalize_source_paths(netlist_text: str, source_paths: Iterable[str]) -> str:
    """Return ``netlist_text`` with each source's absolute directory prefix
    removed (``<dir>/file.v`` -> ``file.v``).

    Returns the input unchanged (same object) when no prefix occurs, so a
    caller can rewrite the file only when something actually changed.
    """
    dirs = {os.path.dirname(os.path.abspath(path)).rstrip("/") for path in source_paths}
    # Longest first so a nested directory is not half-stripped by its parent.
    for directory in sorted((d for d in dirs if d), key=len, reverse=True):
        netlist_text = netlist_text.replace(directory + "/", "")
    return netlist_text


def rewrite_netlist(
    netlist_path: str,
    original_text: str,
    new_text: str,
    source_paths: Iterable[str],
) -> None:
    """Write ``new_text`` (already post-processed by the caller) to
    ``netlist_path`` with source paths normalized out, only if the result
    differs from ``original_text`` -- an unaffected netlist is never touched,
    mtime included.
    """
    final_text = normalize_source_paths(new_text, source_paths)
    if final_text != original_text:
        with open(netlist_path, "w", encoding="utf-8") as handle:
            handle.write(final_text)
