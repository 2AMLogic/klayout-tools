"""Unified diff of a committed netlist against the regenerated one.

Split out of :mod:`klayout_tools.netlist` (issue #5743) so that module stays
under its file-size ratchet; the behaviour is unchanged.
"""

from __future__ import annotations

import difflib

#: Cap on the number of unified-diff lines carried in a ``--check`` payload.
#: A drifted netlist can differ by thousands of lines; the payload's job is
#: to say *that* it drifted and show the head of the difference, not to be a
#: transport for the whole file (both copies are on disk already).
MAX_DIFF_LINES = 200


def unified_diff(committed: bytes, fresh: bytes, output: str) -> tuple[list[str], bool]:
    """Unified diff of the committed netlist against the regenerated one,
    capped at :data:`MAX_DIFF_LINES` lines (second element says whether it
    was truncated)."""
    lines = list(
        difflib.unified_diff(
            committed.decode("utf-8", errors="replace").splitlines(),
            fresh.decode("utf-8", errors="replace").splitlines(),
            fromfile=f"{output} (committed)",
            tofile=f"{output} (regenerated)",
            lineterm="",
        )
    )
    if len(lines) > MAX_DIFF_LINES:
        return lines[:MAX_DIFF_LINES], True
    return lines, False
