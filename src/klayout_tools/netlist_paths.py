"""Rewrites of xschem's ``** sch_path:`` / ``** sym_path:`` comments (split out
of ``netlist.py`` / ``netlist_batch.py`` for the size ratchet; issue #5046)."""

import os
import re
import subprocess
from typing import Any

from .netlist_block import apply_block_mode, space_separate_subckt_ports
from .netlist_port_order import with_port_order

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


def fold_continuations(data: bytes) -> bytes:
    """``data`` with every ``+`` continuation line joined onto the card it
    continues and whitespace runs inside non-comment lines collapsed to
    single spaces (issue #5715): the line layout ``--check`` compares
    (:func:`klayout_tools.netlist._normalize_layout`, issue #3324), shared
    with the writer so ``klt netlist --canonical-wrap`` output is independent
    of the host xschem's wrap column.

    Comment lines (``*``) are left byte-intact: a ``+`` line is never joined
    onto one, and comment content -- xschem's ``**.subckt`` port order
    included -- is not rewritten. Only line layout changes, never a token of
    a non-comment line, so a quoted expression with embedded spaces
    (``ad='int((nf+1)/2) * w/nf * 0.29'``) survives verbatim. Idempotent.
    """
    lines: list[bytes] = []
    prev_joinable = False
    for raw in data.split(b"\n"):
        if raw.lstrip().startswith(b"*"):
            lines.append(raw)
            prev_joinable = False
            continue
        tokens = raw.split()
        if tokens and tokens[0].startswith(b"+") and prev_joinable:
            rest = [tokens[0][1:]] + tokens[1:]
            lines[-1] = b" ".join([lines[-1]] + [t for t in rest if t])
            continue
        lines.append(b" ".join(tokens))
        prev_joinable = bool(tokens)
    return b"\n".join(lines)


def postprocess_netlist(
    data: bytes,
    *,
    block: bool,
    relative_paths: bool,
    schematic: str,
    canonical_wrap: bool = False,
) -> tuple[bytes, dict[str, Any] | None]:
    """Apply the post-xschem netlist rewrites (block mode, port spacing,
    ``--relative-paths``, ``--canonical-wrap``); returns ``(data,
    block_info)``.

    ``schematic`` reaches block mode as well as ``--relative-paths``: the
    cell's subcircuit parameters live in its symbol attributes, not in the
    netlist text (issue #5439), and so does the port order of the restored
    ``.subckt`` header (:func:`with_port_order`, issue #5473).

    ``canonical_wrap`` (issue #5715) applies :func:`fold_continuations` last,
    after the other rewrites, on the local write path. The batch backend
    repeats the path rewrites client-side instead of calling this function,
    and must apply the same fold there -- a canonicalization added only here
    would silently no-op under ``--backend batch``."""
    block_info: dict[str, Any] | None = None
    if block:
        data, block_info = apply_block_mode(data, schematic=schematic)
        block_info = with_port_order(
            block_info, data.decode("utf-8", errors="replace"), schematic
        )
    data = space_separate_subckt_ports(data)
    if relative_paths:
        data = relativize_schematic_paths(data, schematic)
    if canonical_wrap:
        data = fold_continuations(data)
    return data, block_info
