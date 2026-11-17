"""``--pins`` order defines the top ``.SUBCKT`` pin order (issue #5255)."""

from __future__ import annotations

import re
from collections.abc import Sequence


def _apply_pin_order_to_spice(
    netlist_path: str, top_name: str, order: Sequence[str]
) -> None:
    """Issue #5255: reorder ``top_name``'s ``.SUBCKT`` header pins so the
    names in ``order`` (``--pins``, case-insensitive) come first in the given
    order; every other pin keeps its existing relative order. A no-op when
    ``order`` is empty or the header is absent (device-free layout).

    A header pin matches a declared name when **any** of its component labels
    does (issue #5534) -- the same per-label rule ``--pins`` already uses to
    decide *which* nets promote (issue #1687), rather than the whole-token
    comparison this function used before. ``Net.expanded_name()`` joins the
    distinct labels on one net with ``,``, which the SPICE writer respells as
    ``|`` for a node/pin token (see
    :func:`~klayout_tools.extract_parasitics.spice_safe_net_name`), so a
    two-label net reaches this function as ``A|A0``. A single-label pin is the
    one-element case and behaves exactly as before.

    The pin's token text is never rewritten -- only its position moves -- and a
    pin matching several declared names is emitted once, at the position of the
    earliest name it matches (it leaves ``pool`` on that first hit)."""
    if not order:
        return
    with open(netlist_path, encoding="utf-8") as handle:
        lines = handle.read().split("\n")
    header_re = re.compile(rf"^\.subckt\s+{re.escape(top_name)}(\s|$)", re.IGNORECASE)
    start = next((i for i, line in enumerate(lines) if header_re.match(line)), None)
    if start is None:
        return
    last = start
    while last + 1 < len(lines) and lines[last + 1].startswith("+"):
        last += 1
    tokens = " ".join(
        [lines[start]] + [line[1:] for line in lines[start + 1 : last + 1]]
    ).split()
    head, pins = tokens[:2], tokens[2:]
    pool = list(pins)
    first: list[str] = []
    for name in order:
        wanted = name.lower()
        for pin in [
            p for p in pool if wanted in {part.lower() for part in p.split("|")}
        ]:
            pool.remove(pin)
            first.append(pin)
    rebuilt = head + first + pool
    new_lines: list[str] = []
    current = ""
    for tok in rebuilt:
        if current and len(current) + 1 + len(tok) > 80:
            new_lines.append(current)
            current = "+ " + tok
        else:
            current = f"{current} {tok}" if current else tok
    new_lines.append(current)
    lines[start : last + 1] = new_lines
    with open(netlist_path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines))
