"""Request-level ``yosys_timeout_s`` budget for ``klt synthesize``.

Kept out of :mod:`klayout_tools.synthesize` (size / complexity ratchets). The
validated budget is bound for the duration of one ``run_synthesize`` call via
a :class:`~contextvars.ContextVar`, so every ``_run_yosys`` call -- including
those in ``synthesize_arithmetic`` / ``synthesize_memory`` -- honours it
without a new parameter on each helper, and it never leaks into a later call.
"""

from __future__ import annotations

import contextvars
import functools
import math
from collections.abc import Callable
from typing import Any, TypeVar

from ._jsonnum import is_number

#: Upper bound for the request's ``yosys_timeout_s`` (30 min needed by a
#: large generic-``abc`` run, with 2x headroom). The budget is paid per Yosys
#: invocation, so under ``arithmetic: auto`` it multiplies across trials.
YOSYS_TIMEOUT_MAX_S = 3600.0

#: Sentinel default for ``_run_yosys(timeout_s=...)``: "use the run's budget".
UNSET: Any = object()

_YOSYS_TIMEOUT_VAR: contextvars.ContextVar[float | None] = contextvars.ContextVar(
    "klt_synthesize_yosys_timeout_s", default=None
)

_F = TypeVar("_F", bound=Callable[..., Any])


def resolve_timeout(timeout_s: Any, default: float) -> float | None:
    """An explicit ``timeout_s`` wins; ``UNSET`` -> bound budget or ``default``."""
    if timeout_s is not UNSET:
        return timeout_s
    bound = _YOSYS_TIMEOUT_VAR.get()
    return default if bound is None else bound


def bind_yosys_timeout(
    request: dict[str, Any], error_cls: type[Exception]
) -> float | None:
    """Validate ``request["yosys_timeout_s"]`` and bind it for this run.

    Returns the budget, or ``None`` when the request did not set one.
    """
    value = request.get("yosys_timeout_s")
    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not is_number(value)
        or not math.isfinite(value)
        or value <= 0
        or value > YOSYS_TIMEOUT_MAX_S
    ):
        raise error_cls(
            "request.yosys_timeout_s must be a finite number > 0 and <= "
            f"{YOSYS_TIMEOUT_MAX_S:g} (seconds); got {value!r}"
        )
    _YOSYS_TIMEOUT_VAR.set(float(value))
    return float(value)


def timeout_fields(yosys_timeout_s: float | None) -> dict[str, float]:
    """Report/provenance fragment: present only when the request set it."""
    return {} if yosys_timeout_s is None else {"yosys_timeout_s": yosys_timeout_s}


def scoped_yosys_timeout(func: _F) -> _F:
    """Decorator: reset the bound budget around ``func`` (no leak across runs)."""

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        token = _YOSYS_TIMEOUT_VAR.set(None)
        try:
            return func(*args, **kwargs)
        finally:
            _YOSYS_TIMEOUT_VAR.reset(token)

    return wrapper  # type: ignore[return-value]
