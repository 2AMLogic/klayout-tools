"""Shared JSON-number guards used across the ``klt`` verb modules.

Python's ``bool`` is an ``int``, so every verb module that validates request
JSON needs the same "a number (or integer), but not a bool" guard. ``draw.py``,
``socket_check.py``, ``gen_compose_port_request.py``, ``sim.py`` and
``post_route_sta.py`` each carried their own copy, and the copies had already
drifted (one tested finiteness with ``value == value``). They now live here as
the single source of truth, following the precedent set by
``_spice_number.py`` and ``_text.py``. Callers keep their own exception type
and message wording, since those are part of the JSON contract's ``error`` text.

The validator shape drifted the same way: ``sim_op_check.py``,
``economy_whatif.py``, ``sim_twoport.py``, ``probit.py`` and
``sim_stimulus.py`` each carried a private ``_number``, and three of them
called ``math.isfinite`` (or ``float()``) on the raw JSON value -- so a
400-digit integer literal (a Python ``int`` too large for a ``float``)
escaped as an uncaught ``OverflowError`` traceback instead of the module's
error envelope. That shape lives here as :func:`require_finite_number`
(issue #5891), which covers the overflow case through ``is_finite_number``
while each caller keeps its exception type and contract wording.
"""

from __future__ import annotations

import math
from typing import Any, TypeGuard, TypeVar

_E = TypeVar("_E", bound=Exception)

#: Default refusal wording; ``{field}`` and ``{value}`` are filled per call.
_NUMBER_MESSAGE = "{field} must be a number (got {value!r})"
_FINITE_MESSAGE = "{field} must be finite (got {value!r})"


def is_number(value: Any) -> TypeGuard[int | float]:
    """True for a JSON number (int/float), excluding ``bool`` (a Python int)."""
    return not isinstance(value, bool) and isinstance(value, (int, float))


def is_int(value: Any) -> TypeGuard[int]:
    """True for a JSON integer, excluding ``bool`` (a Python int)."""
    return not isinstance(value, bool) and isinstance(value, int)


def is_finite_number(value: Any) -> TypeGuard[int | float]:
    """True for a JSON number that is neither ``bool``, NaN, nor +/-infinity."""
    if not is_number(value):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:  # an int too large for a float
        return False


def require_finite_number(
    value: Any,
    error_cls: type[_E],
    field: str,
    *,
    message: str | None = None,
    number_message: str | None = None,
    finite_message: str | None = None,
) -> float:
    """``value`` as a finite ``float``, raising ``error_cls`` otherwise.

    The shared shape of the per-module ``_number`` validators: refuse a bool
    or non-number, refuse NaN/infinity -- including an integer too large for
    a float, which ``is_finite_number`` catches where a raw ``math.isfinite``
    would escape as an uncaught ``OverflowError`` (issue #5891) -- then
    convert. The caller keeps its JSON-contract wording: ``message``
    overrides both refusals at once, ``number_message`` / ``finite_message``
    each one of the pair.
    """
    refused_as_number = number_message or message or _NUMBER_MESSAGE
    refused_as_finite = finite_message or message or _FINITE_MESSAGE
    if not is_number(value):
        raise error_cls(refused_as_number.format(field=field, value=value))
    if not is_finite_number(value):
        raise error_cls(refused_as_finite.format(field=field, value=value))
    return float(value)
