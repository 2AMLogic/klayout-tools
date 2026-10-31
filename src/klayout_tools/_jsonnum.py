"""Shared JSON-number predicates used across the ``klt`` verb modules.

Python's ``bool`` is an ``int``, so every verb module that validates request
JSON needs the same "a number, but not a bool" guard. ``draw.py``,
``socket_check.py``, ``gen_compose_port_request.py``, ``sim.py`` and
``post_route_sta.py`` each carried their own copy, and the copies had already
drifted (one tested finiteness with ``value == value``). They now live here as
the single source of truth, following the precedent set by
``_spice_number.py`` and ``_text.py``. Callers keep their own exception type
and message wording, since those are part of the JSON contract's ``error`` text.
"""

from __future__ import annotations

import math
from typing import Any


def is_number(value: Any) -> bool:
    """True for a JSON number (int/float), excluding ``bool`` (a Python int)."""
    return not isinstance(value, bool) and isinstance(value, (int, float))


def is_finite_number(value: Any) -> bool:
    """True for a JSON number that is neither ``bool``, NaN, nor +/-infinity."""
    if not is_number(value):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:  # an int too large for a float
        return False
