"""Per-invocation Yosys budget for ``klt synthesize``.

Kept out of :mod:`klayout_tools.synthesize` (size / complexity ratchets). The
validated budget is bound for the duration of one ``run_synthesize`` call via
a :class:`~contextvars.ContextVar`, so every ``_run_yosys`` call -- including
those in ``synthesize_arithmetic`` / ``synthesize_memory`` -- honours it
without a new parameter on each helper, and it never leaks into a later call.

Three override sources resolve in precedence order (issue #5773):
``--yosys-timeout-s`` > ``$KLT_YOSYS_TIMEOUT_S`` > ``request.yosys_timeout_s``
> the 300 s default. :func:`bind_yosys_timeout` owns that resolution; one
validation rule (:func:`validate_budget`) and one cap
(:data:`YOSYS_TIMEOUT_MAX_S`) serve all three.
"""

from __future__ import annotations

import contextvars
import functools
import os
from collections.abc import Callable
from typing import Any, TypeVar

from ._jsonnum import is_finite_number
from .synthesize_abc_timing import _ABC_MODULE_RE as _ABC_MODULE_RE

#: Upper bound for the request's ``yosys_timeout_s`` (30 min needed by a
#: large generic-``abc`` run, with 2x headroom). The budget is paid per Yosys
#: invocation, so under ``arithmetic: auto`` it multiplies across trials.
YOSYS_TIMEOUT_MAX_S = 3600.0

#: Default per-invocation wall-clock timeout for one ``yosys -s <script>`` run
#: (``synthesize._run_yosys``) when no source sets a budget. Generous on
#: purpose: a full ``synth``/``abc`` pass does more work than ``equiv``'s SAT
#: proof, and ``arithmetic: auto`` pays this budget ~10+ times per request.
DEFAULT_YOSYS_TIMEOUT_S = 300.0

#: Report keys ``synthesize.RERUN_BOOKKEEPING_KEYS`` drops from a ``--rerun``
#: diff (issue #5773). The budget is host-level bookkeeping, not a verdict
#: field: the point of the flag / env overrides is that an operator on a
#: loaded box can raise the watchdog on a rerun without touching the committed
#: report or the request, so a different budget on the fresh side is not
#: drift. The synthesis *result* a rerun verifies is budget-independent; a
#: consumer wanting the fresh budget reads the embedded ``fresh`` report.
RERUN_BOOKKEEPING_KEYS: frozenset[str] = frozenset(
    {"yosys_timeout_s", "abc_timeout_s", "stats_path"}
)

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


def validate_budget(value: Any, source_label: str, error_cls: type[Exception]) -> float:
    """One validation rule and one error shape for every budget source.

    ``source_label`` names the source in the error (``--yosys-timeout-s``,
    ``KLT_YOSYS_TIMEOUT_S`` or ``request.yosys_timeout_s``) so a typo'd
    override is a structured error naming where it came from and the single
    :data:`YOSYS_TIMEOUT_MAX_S` cap -- never a silently-ignored value.
    """
    if not is_finite_number(value) or value <= 0 or value > YOSYS_TIMEOUT_MAX_S:
        raise error_cls(
            f"{source_label} must be a finite number > 0 and <= "
            f"{YOSYS_TIMEOUT_MAX_S:g} (seconds); got {value!r}"
        )
    return float(value)


def _validate_override(
    value: str | float, source_label: str, error_cls: type[Exception]
) -> float:
    """Parse flag and environment text, retaining invalid input for the error."""
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            pass
    return validate_budget(value, source_label, error_cls)


def bind_yosys_timeout(
    request: dict[str, Any],
    error_cls: type[Exception],
    flag_value: str | float | None = None,
) -> float | None:
    """Resolve the per-invocation Yosys budget and bind it for this run.

    Precedence: ``--yosys-timeout-s`` (``flag_value``) >
    ``$KLT_YOSYS_TIMEOUT_S`` > ``request["yosys_timeout_s"]`` > the 300 s
    :data:`DEFAULT_YOSYS_TIMEOUT_S` default (a
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
        flag = _validate_override(flag_value, "--yosys-timeout-s", error_cls)
    raw_env = os.environ.get(YOSYS_TIMEOUT_ENV, "").strip()
    env = _validate_override(raw_env, YOSYS_TIMEOUT_ENV, error_cls) if raw_env else None
    request_value = request.get("yosys_timeout_s")
    if request_value is not None:
        request_value = validate_budget(
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
    hint = timeout_diagnostic(stdout) or (
        " (raise --yosys-timeout-s, $KLT_YOSYS_TIMEOUT_S or "
        "request.yosys_timeout_s to allow more time)"
    )
    return message + hint
