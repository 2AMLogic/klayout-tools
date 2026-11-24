"""Shared plain-text helpers used across the ``klt`` verb modules.

``size.py`` (``klt size``, gm/Id sizing, #727) and ``sim.py`` (``klt sim``,
the ngspice PVT corner runner, #96) both need to turn a regex match offset
into the source log line around it, to build a human-readable diagnostic
message from ngspice log text. ``_line_containing`` was previously defined
identically in both modules; it now lives here as the single source of
truth, following the precedent set by ``_paths.py``, ``_layout.py``, and
``_check_result.py`` for verbatim-duplicated verb-module helpers.

``read_text`` (#5755) consolidates the four copies of ``_read_text``
(``pdk``, ``pex_netlist_text``, ``functional_verification_sdf_rename``,
``characterize_leg``) -- one of which had drifted into raising
``UnicodeDecodeError`` instead of degrading to a cache miss on a
non-UTF-8 file.
"""

from __future__ import annotations


def read_text(path: str) -> str | None:
    """``path``'s text as UTF-8 (undecodable bytes replaced), or ``None`` if
    it cannot be read -- callers treat an unreadable file as a cache miss /
    degraded diagnostic rather than an error."""
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return None


def line_containing(text: str, offset: int) -> str:
    """Return the stripped source line of ``text`` that contains ``offset``."""
    start = text.rfind("\n", 0, offset) + 1
    end = text.find("\n", offset)
    if end == -1:
        end = len(text)
    return text[start:end].strip()
