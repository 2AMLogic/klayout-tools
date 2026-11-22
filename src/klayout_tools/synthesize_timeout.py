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
from collections.abc import Callable
from typing import Any, TypeVar

from ._jsonnum import is_finite_number
from .synthesize_abc_timing import _ABC_MODULE_RE as _ABC_MODULE_RE

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
    if not is_finite_number(value) or value <= 0 or value > YOSYS_TIMEOUT_MAX_S:
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


def timeout_diagnostic(stdout: str | bytes | None) -> str:
    """Diagnostic appended to a timeout error when the captured log shows
    Yosys stalled inside ABC (issue #6333).

    Recognises the shape reproduced on an open gf180mcu flow: the run log's
    last non-empty line is Yosys's per-module ABC banner -- the
    ``Extracting gate netlist of module `…'`` form already matched by
    :data:`~klayout_tools.synthesize_abc_timing._ABC_MODULE_RE` -- with no
    ABC output after it. That reads as "this ABC build cannot process this
    liberty", not "the design is too big for the budget": the
    :class:`~klayout_tools.synthesize.SynthesizeError` names it so the
    reader does not shrink the design or raise a budget that cannot finish.

    ``stdout`` is whatever ``subprocess.TimeoutExpired`` captured so far
    (``str``, ``bytes`` -- decoded as :func:`klayout_tools.synthesize.
    _write_yosys_log` decodes -- or ``None``); any of those is accepted and
    an empty/absent log simply yields ``""``. Conservative by construction:
    a banner followed by *any* later output means ABC made progress past
    extraction, so no diagnostic is offered.
    """
    if isinstance(stdout, bytes):
        stdout = stdout.decode("utf-8", errors="replace")
    lines = [line for line in (stdout or "").splitlines() if line.strip()]
    if not lines or not _ABC_MODULE_RE.search(lines[-1]):
        return ""
    return (
        " -- hint: ABC produced no output after its gate-netlist extraction"
        " banner -- this points to an ABC/liberty incompatibility in the"
        " yosys build on PATH rather than a design-size problem, so raising"
        " yosys_timeout_s is unlikely to help; check the yosys build against"
        " the environment guidance in docs/cli/synthesize.md"
    )


def timeout_message(
    timeout_s: float | None, script_path: str, log_path: str, stdout: str | bytes | None
) -> str:
    """Name the timeout, saved log and the next step for the observed phase."""
    message = (
        f"yosys did not complete within {timeout_s}s (script '{script_path}')"
        " -- process killed (a run hung on yosys-abc's stdout has its abc"
        f" child killed with the whole process group) -- log '{log_path}'"
    )
    hint = (
        timeout_diagnostic(stdout)
        or " (raise request.yosys_timeout_s to allow more time)"
    )
    return message + hint
