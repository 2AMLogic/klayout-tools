"""Per-invocation Yosys budget for ``klt synthesize``.

Kept out of :mod:`klayout_tools.synthesize` (size / complexity ratchets). The
validated budget is bound for the duration of one ``run_synthesize`` call via
a :class:`~contextvars.ContextVar`, so every ``_run_yosys`` call -- including
those in ``synthesize_arithmetic`` / ``synthesize_memory`` -- honours it
without a new parameter on each helper, and it never leaks into a later call.

Three override sources resolve in precedence order (issue #5773):
``--yosys-timeout-s`` > ``$KLT_YOSYS_TIMEOUT_S`` > ``request.yosys_timeout_s``
> the 300 s default. :func:`bind_yosys_timeout` owns that resolution; one
validation rule (:func:`_validate_yosys_timeout`) and one cap
(:data:`YOSYS_TIMEOUT_MAX_S`) serve all three.
"""

from __future__ import annotations

import contextvars
import functools
import math
import os
from collections.abc import Callable
from typing import Any, TypeVar

from ._jsonnum import is_number

#: Upper bound for the request's ``yosys_timeout_s`` (30 min needed by a
#: large generic-``abc`` run, with 2x headroom). The budget is paid per Yosys
#: invocation, so under ``arithmetic: auto`` it multiplies across trials.
YOSYS_TIMEOUT_MAX_S = 3600.0

#: Host-level budget override (``--yosys-timeout-s`` and
#: ``request.yosys_timeout_s`` are the other two sources; see
#: :func:`bind_yosys_timeout` for the precedence). A property of the *host*,
#: not of the design -- the same rationale as ``KLT_SIM_MAX_WORKERS`` -- so
#: an operator on a loaded shared box (or one driving ``--check --rerun``
#: under a fixed argv) can raise the watchdog without editing the request.
YOSYS_TIMEOUT_ENV = "KLT_YOSYS_TIMEOUT_S"

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


def _validate_yosys_timeout(
    value: Any, source_label: str, error_cls: type[Exception]
) -> float:
    """One validation rule and one error shape for every budget source.

    ``source_label`` names the source in the error (``--yosys-timeout-s``,
    ``KLT_YOSYS_TIMEOUT_S`` or ``request.yosys_timeout_s``) so a typo'd
    override is a structured error naming where it came from and the single
    :data:`YOSYS_TIMEOUT_MAX_S` cap -- never a silently-ignored value.
    """
    if (
        isinstance(value, bool)
        or not is_number(value)
        or not math.isfinite(value)
        or value <= 0
        or value > YOSYS_TIMEOUT_MAX_S
    ):
        raise error_cls(
            f"{source_label} must be a finite number > 0 and <= "
            f"{YOSYS_TIMEOUT_MAX_S:g} (seconds); got {value!r}"
        )
    return float(value)


def _env_yosys_timeout(error_cls: type[Exception]) -> float | None:
    """The host-level ``$KLT_YOSYS_TIMEOUT_S`` override, or ``None``.

    Read from the environment on every call -- never cached at import time
    -- so a library caller can set it between two ``run_synthesize``
    invocations and a test can set/clear it without a reset hook. Unset, or
    set to an empty/whitespace-only string (what ``KLT_YOSYS_TIMEOUT_S=``
    in a shell env file produces), means "no override": behaviour is then
    identical to a host without the variable. Any other value is validated
    by :func:`_validate_yosys_timeout` -- a non-numeric string is passed
    through as-is so the error shows what the operator actually typed.
    """
    raw = os.environ.get(YOSYS_TIMEOUT_ENV)
    if raw is None or not raw.strip():
        return None
    text = raw.strip()
    try:
        return _validate_yosys_timeout(float(text), YOSYS_TIMEOUT_ENV, error_cls)
    except ValueError:
        return _validate_yosys_timeout(text, YOSYS_TIMEOUT_ENV, error_cls)


def bind_yosys_timeout(
    request: dict[str, Any],
    error_cls: type[Exception],
    flag_value: float | None = None,
) -> float | None:
    """Resolve the per-invocation Yosys budget and bind it for this run.

    Precedence: ``--yosys-timeout-s`` (``flag_value``) >
    ``$KLT_YOSYS_TIMEOUT_S`` > ``request["yosys_timeout_s"]`` > the 300 s
    :data:`klayout_tools.synthesize.DEFAULT_YOSYS_TIMEOUT_S` default (a
    ``None`` return means no override anywhere, so the default applies).
    Env beats the request field because the variable is a property of the
    *host*, not of the design -- an operator on a loaded box with a
    committed (fixed) request document must still be able to raise the
    watchdog, which is exactly the fixed-argv/fixed-request ``--check``
    gate case; the explicit flag beats env as the most deliberate act.
    Unlike ``KLT_SIM_MAX_WORKERS`` (a clamping *cap*) this is a straight
    override: every provided source is validated, and an out-of-range one
    is refused with an error naming that source and the single
    :data:`YOSYS_TIMEOUT_MAX_S` cap.

    Returns the budget in effect (``None`` when every source is unset).
    """
    flag: float | None = None
    if flag_value is not None:
        flag = _validate_yosys_timeout(flag_value, "--yosys-timeout-s", error_cls)
    env = _env_yosys_timeout(error_cls)
    request_value = request.get("yosys_timeout_s")
    if request_value is not None:
        request_value = _validate_yosys_timeout(
            request_value, "request.yosys_timeout_s", error_cls
        )
    budget = flag if flag is not None else env if env is not None else request_value
    if budget is not None:
        _YOSYS_TIMEOUT_VAR.set(budget)
    return budget


def timeout_fields(yosys_timeout_s: float | None) -> dict[str, float]:
    """Report/provenance fragment: present only when an override (flag, env
    var or request field) set the budget."""
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
