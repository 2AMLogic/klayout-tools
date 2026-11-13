"""Rewrites of xschem's ``** sch_path:`` / ``** sym_path:`` comments (split out
of ``netlist.py`` / ``netlist_batch.py`` for the size ratchet; issue #5046)."""

import os
import re
import subprocess
from typing import Any

from .netlist_block import apply_block_mode, space_separate_subckt_ports

#: xschem writes the absolute path of every schematic and symbol it expands
#: into a SPICE export as a comment line (``** sch_path: /abs/x.sch``,
#: ``** sym_path: /abs/x.sym``). Those lines name the *checkout*, not the
#: circuit, so they differ between any two checkouts of the same schematic
#: (issue #3010). Group 1 is the line up to the path, group 2 the path.
_PATH_COMMENT_RE = re.compile(
    rb"^(\*\*[ \t]*(?:sch_path|sym_path):[ \t]*)(.*?)[ \t]*\r?$", re.MULTILINE
)


def path_comment_root(schematic: str) -> str:
    """Root ``--relative-paths`` relativizes against (issue #5046): the
    schematic's git toplevel, else the schematic's parent directory."""
    parent = os.path.dirname(os.path.realpath(schematic)) or "."
    try:
        result = subprocess.run(
            ["git", "-C", parent, "rev-parse", "--show-toplevel"],
            capture_output=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return parent
    if result.returncode != 0:
        return parent
    top = result.stdout.decode("utf-8", errors="surrogateescape").strip()
    return os.path.realpath(top) if top else parent


def relativize_schematic_paths(data: bytes, schematic: str) -> bytes:
    """:func:`relativize_path_comments` against :func:`path_comment_root`."""
    return relativize_path_comments(data, path_comment_root(schematic))


def relativize_path_comments(data: bytes, root: str) -> bytes:
    """``data`` with every ``** sch_path:`` / ``** sym_path:`` comment
    rewritten relative to ``root`` (issue #5046), so regenerating the same
    sources from any checkout is byte-identical. Paths outside ``root``
    (e.g. PDK symbols) are reduced to their basename, the form
    :func:`_normalize_path_comments` uses. Both the given and the resolved
    ``root`` are tried, so a symlinked checkout still relativizes."""
    roots = {os.path.abspath(root), os.path.realpath(root)}
    prefixes = [
        r.rstrip("/").encode("utf-8", errors="surrogateescape") + b"/"
        for r in sorted(roots, key=len, reverse=True)
    ]

    def _rel(match: re.Match[bytes]) -> bytes:
        path = match.group(2)
        eol = b"\r" if match.group(0).endswith(b"\r") else b""
        for prefix in prefixes:
            if path.startswith(prefix) and len(path) > len(prefix):
                return match.group(1) + path[len(prefix) :] + eol
        stripped = path.rstrip(b"/\\")
        base = re.split(rb"[/\\]", stripped)[-1] if stripped else stripped
        return match.group(1) + base + eol

    return _PATH_COMMENT_RE.sub(_rel, data)


def _rewrite_runner_paths(
    data: bytes, runner_roots: list[str], local_root: str
) -> bytes:
    """Rewrite xschem's ``** sch_path:`` / ``** sym_path:`` comments that
    point into the runner's scratch tree so they point into ``local_root``
    -- the paths a local export from this checkout would have written.
    Paths outside the tree (the runner's PDK symbols) are left alone."""
    if not runner_roots:
        return data
    targets = [root.encode("utf-8", errors="surrogateescape") for root in runner_roots]
    local = local_root.rstrip("/").encode("utf-8", errors="surrogateescape")

    def _rewrite(match: Any) -> bytes:
        path = match.group(2)
        for target in targets:
            if path == target or path.startswith(target + b"/"):
                return match.group(1) + local + path[len(target) :]
        return match.group(0)

    return _PATH_COMMENT_RE.sub(_rewrite, data)


def postprocess_netlist(
    data: bytes, *, block: bool, relative_paths: bool, schematic: str
) -> tuple[bytes, dict[str, Any] | None]:
    """Apply the post-xschem netlist rewrites (block mode, port spacing,
    ``--relative-paths``); returns ``(data, block_info)``.

    ``schematic`` reaches block mode as well as ``--relative-paths``: the
    cell's subcircuit parameters live in its symbol attributes, not in the
    netlist text (issue #5439)."""
    block_info: dict[str, Any] | None = None
    if block:
        data, block_info = apply_block_mode(data, schematic=schematic)
    data = space_separate_subckt_ports(data)
    if relative_paths:
        data = relativize_schematic_paths(data, schematic)
    return data, block_info
