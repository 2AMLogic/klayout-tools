"""Shared plain-text helpers used across the ``klt`` verb modules.

``sim.py`` (``klt sim``, the ngspice PVT corner runner, #96) and ``size.py``
(``klt size``, gm/Id sizing, #727) both need to turn a regex match offset
into the source log line around it, to build a human-readable diagnostic
message from ngspice log text. ``_line_containing`` was previously defined
identically in both modules; it now lives here as the single source of
truth, following the precedent set by ``_paths.py``, ``_layout.py``, and
``_check_result.py`` for verbatim-duplicated verb-module helpers.
"""

from __future__ import annotations


def line_containing(text: str, offset: int) -> str:
    """Return the stripped source line of ``text`` that contains ``offset``."""
    start = text.rfind("\n", 0, offset) + 1
    end = text.find("\n", offset)
    if end == -1:
        end = len(text)
    return text[start:end].strip()
