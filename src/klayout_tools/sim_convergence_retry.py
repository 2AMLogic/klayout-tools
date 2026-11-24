"""Convergence-retry policy and corner classification for ``klt sim``."""

from __future__ import annotations

from typing import Any

from ._jsonnum import is_int, is_number
from .sim_errors import SimError

#: Error-severity diagnostic codes that can accompany a ``nonconvergence``
#: failure without making it something other than one (the measurements and
#: vectors a failed solve never produced, plus ``timestep_too_small`` and
#: ``analysis_aborted``, which ngspice emits when the solver gives up --
#: issue #4983). Any *other* error-severity code
#: -- ``singular_matrix``, ``netlist``, ``timeout``, ``engine_killed``, ... --
#: means a different failure class, which is never retried (issue #2973).
CONVERGENCE_CONSEQUENCE_CODES = frozenset(
    {
        "nonconvergence",
        "measurement",
        "no_such_vector",
        "timestep_too_small",
        "analysis_aborted",
    }
)


def _error_codes(corner: dict[str, Any]) -> set[str]:
    return {d["code"] for d in corner["diagnostics"] if d["severity"] == "error"}


def is_retryable_convergence_failure(corner: dict[str, Any]) -> bool:
    if corner["status"] != "error":
        return False
    codes = _error_codes(corner)
    return "nonconvergence" in codes and codes <= CONVERGENCE_CONSEQUENCE_CODES


def note_not_retried(corner: dict[str, Any]) -> None:
    """Append the ``convergence_retry`` info diagnostic to an errored corner
    that was not retried, naming the error classes that ruled it out."""
    if corner["status"] != "error":
        return
    codes = ", ".join(sorted(_error_codes(corner))) or "none"
    corner["diagnostics"].append(
        {
            "severity": "info",
            "code": "convergence_retry",
            "message": (
                f"not retried: error classes {codes} are not a nonconvergence failure"
            ),
        }
    )


#: ``options.retry_on_convergence_error`` (issue #2973): the solver options a
#: retried unit runs with when the object form leaves a key out. ``gear``
#: integration and a looser ``reltol`` are the standard first remedies for
#: ngspice's "time step too small".
DEFAULT_CONVERGENCE_RETRY: dict[str, Any] = {
    "max_attempts": 1,
    "reltol": 0.003,
    "method": "gear",
}
_CONVERGENCE_RETRY_METHODS = ("trap", "gear")


def _validate_convergence_retry(value: Any) -> dict[str, Any] | None:
    """Validate and normalise ``options.retry_on_convergence_error`` (issue
    #2973).

    ``None``/absent/``false`` -> ``None``: no retry, today's behaviour
    exactly. ``true`` -> :data:`DEFAULT_CONVERGENCE_RETRY`. An object may set
    ``max_attempts`` (integer >= 1), ``reltol`` (number in ``(0, 1)``) and
    ``method`` (``"trap"``/``"gear"``); omitted keys take the default and an
    unknown key is an application error (``SimError``) rather than a silent
    no-op, like :func:`_validate_fail_on_diagnostic`.
    """
    if value is None or value is False:
        return None
    if value is True:
        return dict(DEFAULT_CONVERGENCE_RETRY)
    if not isinstance(value, dict):
        raise SimError(
            "options.retry_on_convergence_error must be a boolean or an object "
            'like {"max_attempts": 1, "reltol": 0.003, "method": "gear"}'
        )
    unknown = sorted(set(value) - set(DEFAULT_CONVERGENCE_RETRY))
    if unknown:
        raise SimError(
            "options.retry_on_convergence_error has unknown key(s): "
            f"{', '.join(unknown)} (known: {', '.join(DEFAULT_CONVERGENCE_RETRY)})"
        )
    resolved = {**DEFAULT_CONVERGENCE_RETRY, **value}
    attempts = resolved["max_attempts"]
    if not is_int(attempts) or attempts < 1:
        raise SimError(
            "options.retry_on_convergence_error.max_attempts must be an integer >= 1"
        )
    reltol = resolved["reltol"]
    if not is_number(reltol) or not 0 < reltol < 1:
        raise SimError(
            "options.retry_on_convergence_error.reltol must be a number "
            "between 0 and 1 (exclusive)"
        )
    if resolved["method"] not in _CONVERGENCE_RETRY_METHODS:
        raise SimError(
            "options.retry_on_convergence_error.method must be one of: "
            + ", ".join(_CONVERGENCE_RETRY_METHODS)
        )
    return resolved
