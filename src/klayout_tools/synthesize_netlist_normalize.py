"""Normalize host source paths out of a Yosys-written gate-level netlist
(issue #4872, extended for the repo-relative spelling by issue #5594).

Yosys names some internal nets after the path it was given for the source
file, e.g. ``\\f$func$/abs/dir/f.v:3$2.x`` (seen with sv2v output that has
functions with casts). ``klt synthesize`` hands Yosys each source in the
spelling :func:`~klayout_tools.synthesize._script_path_text` embedded in
the script: absolute when the source lies outside the run's repo root,
repo-relative (issue #1844) when it lies inside -- and Yosys records that
text verbatim. Either way the written netlist would embed a host- or
repo-dependent directory, so identical requests from different build
roots (or, with a defaulted ``run_id``, even from the same directory)
would differ byte for byte and leak host paths.

:func:`normalize_source_paths` is a pure text transform: it drops each
source's directory prefix in *both* spellings -- the absolute directory
and, when ``repo_root`` is given and the source resolves inside it, the
repo-relative directory -- leaving ``f.v:3`` in the name. Both branches
collapse to the same bare basename, so a netlist built inside a repo is
byte-identical to one built outside it.
"""

from __future__ import annotations

import os
from collections.abc import Iterable

from . import env_provenance


def normalize_source_paths(
    netlist_text: str,
    source_paths: Iterable[str],
    *,
    repo_root: str | None = None,
) -> str:
    """Return ``netlist_text`` with each source's directory prefix removed
    (``<dir>/file.v`` -> ``file.v``), in whichever spelling Yosys recorded:
    the absolute directory always, and the ``repo_root``-relative directory
    as well when the source resolves inside ``repo_root`` (via the same
    :func:`~klayout_tools.env_provenance.repo_relative_path`
    ``_script_path_text`` embeds -- one normalizer, not two).

    Returns the input unchanged (same object) when no prefix occurs, so a
    caller can rewrite the file only when something actually changed.
    """
    paths = list(source_paths)
    dirs = {os.path.dirname(os.path.abspath(path)).rstrip("/") for path in paths}
    if repo_root is not None:
        for path in paths:
            normalized = env_provenance.repo_relative_path(path, repo_root=repo_root)
            if normalized["scope"] == "repo" and isinstance(normalized["path"], str):
                # POSIX separators; a source at the repo root has an empty
                # dirname, which the guard below must never turn into a
                # strip of "/".
                dirs.add(os.path.dirname(normalized["path"]).rstrip("/"))
    # Longest first so a nested directory is not half-stripped by its parent.
    for directory in sorted((d for d in dirs if d), key=len, reverse=True):
        netlist_text = netlist_text.replace(directory + "/", "")
    return netlist_text


def rewrite_netlist(
    netlist_path: str,
    original_text: str,
    new_text: str,
    source_paths: Iterable[str],
    *,
    repo_root: str | None = None,
) -> None:
    """Write ``new_text`` (already post-processed by the caller) to
    ``netlist_path`` with source paths normalized out, only if the result
    differs from ``original_text`` -- an unaffected netlist is never touched,
    mtime included.
    """
    final_text = normalize_source_paths(new_text, source_paths, repo_root=repo_root)
    if final_text != original_text:
        with open(netlist_path, "w", encoding="utf-8") as handle:
            handle.write(final_text)
