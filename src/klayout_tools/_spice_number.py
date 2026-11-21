"""Shared SPICE engineering-notation number parsing (``1u``, ``5meg``, ``10k``).

Six modules (``netlist_normalize``, ``op_sanity``, ``sim``, ``sim_noise``,
``sim_stimulus``, ``sim_supply_waveform``) each carried their own suffix table
and number regex, and the copies drifted (``mil`` was misread as ``1e-3`` in
three of them). :func:`parse` is the single place the table lives now: the
superset of the old copies, longest-match first so ``meg``/``mil`` are not
shadowed by ``m``. Trailing unit letters (``10uF``, ``1kohm``) are ignored.
"""

from __future__ import annotations

import re

#: ``(mantissa, trailing letters)`` of a plain numeric SPICE literal.
NUMBER_RE = re.compile(r"^([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)([a-zA-Z]*)$")

#: Multiplier suffixes, longest-match first (``meg``/``mil`` before ``m``).
SUFFIXES: tuple[tuple[str, float], ...] = (
    ("meg", 1e6),
    ("mil", 25.4e-6),
    ("t", 1e12),
    ("g", 1e9),
    ("k", 1e3),
    ("m", 1e-3),
    ("u", 1e-6),
    ("n", 1e-9),
    ("p", 1e-12),
    ("f", 1e-15),
    ("a", 1e-18),
)


def suffix_scale(suffix: str) -> float:
    """Multiplier of the trailing letters of a SPICE literal (1.0 if none)."""
    lowered = suffix.lower()
    for name, scale in SUFFIXES:
        if lowered.startswith(name):
            return scale
    return 1.0


def parse(token: str) -> float | None:
    """Value of a plain numeric SPICE literal, or ``None`` if it is not one."""
    match = NUMBER_RE.match(token.strip())
    if match is None:
        return None
    return float(match.group(1)) * suffix_scale(match.group(2))
